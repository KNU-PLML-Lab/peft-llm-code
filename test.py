import logging
import re
from collections import defaultdict

import evaluate
import torch

from peft import PeftModel
from torch.utils.data import DataLoader
from transformers import \
    AutoTokenizer, \
    default_data_collator, \
    StoppingCriteriaList, \
    StoppingCriteria

from utils import *

logger = logging.getLogger(__name__)

EOF_STRINGS = ["<|endoftext|>", "</s>"]


def load_model_and_tokenizer(args):
    if args.training_method == "ft":
        model = GENERATION_MODEL_CLS[args.model_type].from_pretrained(args.model_name_or_path).to(args.device)
        tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path)
    else:
        inference_model = GENERATION_MODEL_CLS[args.model_type].from_pretrained(args.model_name_or_path,
                                                                                torch_dtype=torch.float16)
        tokenizer = AutoTokenizer.from_pretrained(args.model_name_or_path)
        model = PeftModel.from_pretrained(inference_model, args.lora_adapter_path).to(args.device)
        model.print_trainable_parameters()

    if getattr(tokenizer, "pad_token_id") is None:
        tokenizer.pad_token_id = tokenizer.eos_token_id
        model.config.pad_token_id = model.config.eos_token_id

    if "incoder" in args.model_name:
        tokenizer.eos_token_id = 2
        tokenizer.pad_token_id = 1

    return model, tokenizer


class EndOfFunctionCriteria(StoppingCriteria):
    """Custom `StoppingCriteria` which checks if all generated functions in the batch are completed."""

    def __init__(self, start_length, eof_strings, tokenizer):
        self.start_length = start_length
        self.eof_strings = eof_strings
        self.tokenizer = tokenizer

    def __call__(self, input_ids, scores, **kwargs):
        """Returns true if all generated sequences contain any of the end-of-function strings."""
        decoded_generations = self.tokenizer.batch_decode(input_ids[:, self.start_length:])
        done = []
        for decoded_generation in decoded_generations:
            done.append(any([stop_string in decoded_generation for stop_string in self.eof_strings]))
        return all(done)


def test_conala_code_generation(args):
    dataset = load_conala_dataset(args)
    test_dataset = dataset["test"]

    model, tokenizer = load_model_and_tokenizer(args)
    if args.model_type == "decoder":
        tokenizer.padding_side = "left"

    def preprocess_function_dec(example):
        model_inputs = tokenizer(example["rewritten_intent"] + "\n",
                                 truncation=True,
                                 padding="max_length",
                                 max_length=args.conala_max_input_length)
        labels = tokenizer(example["snippet"],
                           truncation=True,
                           padding="max_length",
                           max_length=args.conala_max_target_length)["input_ids"]
        model_inputs["labels"] = labels

        return model_inputs

    def preprocess_function_encdec(example):
        model_inputs = tokenizer(example["rewritten_intent"] + "\n",
                                 truncation=True,
                                 padding="max_length",
                                 max_length=args.conala_max_input_length,
                                 add_special_tokens=True)
        labels = tokenizer(example["snippet"],
                           truncation=True,
                           padding="max_length",
                           max_length=args.conala_max_target_length,
                           add_special_tokens=True)["input_ids"]
        model_inputs["labels"] = labels

        return model_inputs

    preprocess_function = preprocess_function_dec if args.model_type == "decoder" else preprocess_function_encdec
    test_dataset = test_dataset.map(preprocess_function,
                                    num_proc=args.num_workers,
                                    remove_columns=[cname for cname in test_dataset.column_names if
                                                    cname not in ["input_ids", "attention_mask", "labels"]],
                                    desc="Generating samples features.")
    dataloader = DataLoader(test_dataset,
                            batch_size=args.batch_size,
                            collate_fn=default_data_collator,
                            pin_memory=True)

    predictions = []
    references = []
    for batch in tqdm(dataloader, total=len(test_dataset) // args.batch_size):
        batch = {k: v.to(args.device) for k, v in batch.items()}
        with torch.no_grad():
            batch_generation = model.generate(
                input_ids=batch["input_ids"],
                attention_mask=batch["attention_mask"],
                use_cache=True,
                temperature=args.temperature,
                num_beams=args.beam_size,
                max_new_tokens=args.conala_max_target_length,
                stopping_criteria=StoppingCriteriaList(
                    [EndOfFunctionCriteria(batch["input_ids"].shape[1], EOF_STRINGS, tokenizer)]
                )
            )
            if args.model_type == "decoder":
                batch_generated_tokens = tokenizer.batch_decode(batch_generation[:, batch["input_ids"].shape[1]:],
                                                                skip_special_tokens=True)
            else:
                batch_generated_tokens = tokenizer.batch_decode(batch_generation, skip_special_tokens=True)
            batch_references = tokenizer.batch_decode(batch["labels"], skip_special_tokens=True)
            if "incoder" in args.model_name:
                # somehow the pad tokens do not get filtered when decoding with InCoder
                batch_references = [ref.replace("<pad>", "") for ref in batch_references]
            predictions += [generated_tokens for generated_tokens in batch_generated_tokens]
            references += [tokens for tokens in batch_references]

            print("*" * 100)
            print(batch_generated_tokens[0])
            print("-" * 100)
            print(batch_references[0])
            print("*" * 100)

    if args.training_method == "lora":
        args.run_dir = args.lora_adapter_path
    logger.info(f"Exporting test predictions in directory {args.run_dir}.")
    with open(os.path.join(f"{args.run_dir}/predictions.txt"), "w", encoding="utf-8") as fpred, \
            open(os.path.join(f"{args.run_dir}/references.txt"), "w", encoding="utf-8") as fref:
        for prediction, reference, dataset in zip(predictions, references, test_dataset):
            fpred.write(prediction.replace("\n", " ") + "\n")
            fref.write(reference.replace("\n", " ") + "\n")


HUMAN_EVAL_EOF_STRINGS = ["\nclass", "\ndef", "\n#", "\n@", "\nprint", "\nif"]

def remove_last_block(string):
    """Remove the last block of the code containing EOF_STRINGS"""
    string_list = re.split("(%s)" % "|".join(HUMAN_EVAL_EOF_STRINGS), string)
    print(string_list)
    # last string should be ""
    return "".join(string_list[:-2])


def test_human_eval(args):
    os.environ["HF_ALLOW_CODE_EVAL"] = "1"

    human_eval = load_dataset("openai_humaneval")
    code_eval_metric = evaluate.load("code_eval")

    model, tokenizer = load_model_and_tokenizer(args)
    if args.model_type == "decoder":
        tokenizer.padding_side = "left"

    human_eval = human_eval.map(lambda e: tokenizer(e["prompt"] + "\n"),
                                num_proc=args.num_workers,
                                desc="Generating samples features.")["test"].select(range(2))

    dataloader = DataLoader(human_eval,
                            batch_size=1,
                            collate_fn=default_data_collator,
                            pin_memory=True)

    gen_token_dict = defaultdict(list)
    for step, sample in tqdm(enumerate(dataloader), total=len(human_eval)):
        with torch.no_grad():
            generated_sequences = model.generate(
                input_ids=sample["input_ids"].to(args.device),
                use_cache=True,
                do_sample=True,
                temperature=0.2,
                max_new_tokens=args.human_eval_max_new_tokens,
                num_return_sequences=2,
                stopping_criteria=StoppingCriteriaList(
                    [EndOfFunctionCriteria(sample["input_ids"].shape[1], HUMAN_EVAL_EOF_STRINGS, tokenizer)]
                )
            )
            generated_sequences = generated_sequences.cpu().numpy()
            for s in generated_sequences:
                new_tokens = s[sample["input_ids"].shape[1]:]
                new_tokens_decoded = tokenizer.decode(new_tokens, skip_special_tokens=True, clean_up_tokenization_spaces=True)
                string_list = re.split("(%s)" % "|".join(HUMAN_EVAL_EOF_STRINGS), new_tokens_decoded)
                print(string_list)

                gen_token_dict[step].append(s)
                print("-" * 100)
    """
    code_gens = [[] for _ in range(len(human_eval))]
    for task, generated_tokens in gen_token_dict.items():
        for s in generated_tokens:
            gen_code = tokenizer.decode(s, skip_special_tokens=True, clean_up_tokenization_spaces=True)
            print(gen_code)
            print("-" * 100)
            print(remove_last_block(gen_code))
            print("*" * 100)
            # code_gens[task].append(remove_last_block(gen_code))
    """
