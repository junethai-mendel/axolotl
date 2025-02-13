"""
HF Chat Templates prompt strategy
"""

import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional

from transformers import ProcessorMixin

from axolotl.prompt_tokenizers import PromptTokenizingStrategy
from axolotl.prompters import IGNORE_TOKEN_ID, Prompter
from axolotl.utils.chat_templates import get_chat_template_from_config

# Configure the logger
LOG = logging.getLogger("axolotl")
LOG.setLevel(logging.INFO)


class ChatTemplatePrompter(Prompter):
    """Prompter for HF chat templates"""

    def __init__(
        self,
        tokenizer,
        processor=None,
        chat_template=None,
        max_length=2048,
        message_field_role: str = "role",
        message_field_content: str = "content",
        message_field_training: Optional[str] = None,
        message_field_training_detail: Optional[str] = None,
        roles: Optional[Dict[str, List[str]]] = None,
        drop_system_message: bool = False,
    ):
        if roles:
            self.roles = {s: t for t, sources in roles.items() for s in sources}
        else:
            self.roles = {
                "human": "user",
                "user": "user",
                "assistant": "assistant",
                "gpt": "assistant",
                "system": "system",
                "tool": "tool",
            }

        self.message_field_role = message_field_role
        self.message_field_content = message_field_content
        self.message_field_training = message_field_training
        self.message_field_training_detail = message_field_training_detail
        self.tokenizer = tokenizer
        self.processor: ProcessorMixin = processor
        self.chat_template = chat_template
        self.max_length = max_length
        self.drop_system_message = drop_system_message

    def build_prompt(self, conversation, add_generation_prompt=False, images=None):
        if self.processor:
            text = self.processor.apply_chat_template(
                conversation,
                chat_template=self.chat_template,
                tokenize=False,
                add_generation_prompt=add_generation_prompt,
            )
            batch = self.processor(
                text=text,
                images=images,
                return_tensors="pt",
            )
            # workaround since processor works in batches instead of single examples
            for k, val in batch.items():
                if k in ["pixel_values"]:
                    batch[k] = val.tolist()
                else:
                    batch[k] = val.squeeze().tolist()
            return batch

        return self.tokenizer.apply_chat_template(
            conversation,
            add_generation_prompt=add_generation_prompt,
            chat_template=self.chat_template,
        )

    def get_offsets_for_train_detail(
        self, text: str, train_details: List[Dict], mask_untrainable: bool = True
    ) -> List[int]:
        tokenized_output = self.tokenizer(
            text, return_offsets_mapping=True, add_special_tokens=False
        )
        tokens = tokenized_output.tokens()
        token_offsets = tokenized_output["offset_mapping"]

        LOG.debug(f"Tokenizing text: {text}")
        LOG.debug(f"Tokens: {tokens}")
        # Adjust the end offsets. For some reason by default they are set to the same value as the start offsets.
        for i in range(len(token_offsets) - 1):
            token_offsets[i] = (token_offsets[i][0], token_offsets[i + 1][0] - 1)
        # Ensure the last token's end offset is set correctly
        token_offsets[-1] = (token_offsets[-1][0], len(text) - 1)
        LOG.debug(f"Token offsets: {token_offsets}")

        # Initialize all offsets as IGNORE_TOKEN_ID (not trained)
        result = [IGNORE_TOKEN_ID] * len(token_offsets)

        # Adjust train_details to align with token boundaries
        adjusted_train_details = self.adjust_train_details(train_details, token_offsets)

        for idx, (start, end) in enumerate(token_offsets):
            for detail in adjusted_train_details:
                # Check if the token is completely within the detail's range
                if start >= detail["begin_offset"] and end <= detail["end_offset"]:
                    if detail["train"] or not mask_untrainable:
                        result[idx] = start
                        LOG.debug(f"Token {idx} ({tokens[idx]}) marked for training")
                    else:
                        LOG.debug(
                            f"Token {idx} ({tokens[idx]}) marked as non-trainable"
                        )
                elif start < detail["end_offset"] and end > detail["begin_offset"]:
                    # Token partially overlaps with detail, always mark as non-trainable
                    LOG.debug(
                        f"Token {idx} ({tokens[idx]}) partially overlaps detail, marked as non-trainable"
                    )

        LOG.debug(f"Final result: {result}")
        return result

    def adjust_train_details(
        self, train_details: List[Dict], token_offsets: List[tuple]
    ) -> List[Dict]:
        adjusted_details = []
        for detail in train_details:
            begin_offset = detail["begin_offset"]
            end_offset = detail["end_offset"]

            # Find the first token that starts after or at the begin_offset
            begin_token = next(
                (
                    i
                    for i, (t_start, t_end) in enumerate(token_offsets)
                    if t_start >= begin_offset
                ),
                len(token_offsets),
            )
            if begin_token > 0 and token_offsets[begin_token - 1][1] > begin_offset:
                begin_token -= 1

            # Find the last token that ends before or at the end_offset
            end_token = next(
                (
                    i
                    for i in range(len(token_offsets) - 1, -1, -1)
                    if token_offsets[i][1] <= end_offset
                ),
                -1,
            )
            if (
                end_token < len(token_offsets) - 1
                and token_offsets[end_token + 1][0] < end_offset
            ):
                end_token += 1

            if begin_token <= end_token:
                adjusted_begin = token_offsets[begin_token][0]
                adjusted_end = token_offsets[end_token][1]

                if adjusted_begin != begin_offset or adjusted_end != end_offset:
                    LOG.warning(
                        f"Adjusting detail offsets: ({begin_offset}, {end_offset}) -> ({adjusted_begin}, {adjusted_end})"
                    )

                adjusted_details.append(
                    {
                        "begin_offset": adjusted_begin,
                        "end_offset": adjusted_end,
                        "train": detail["train"],
                    }
                )
            else:
                LOG.warning(
                    f"Could not adjust detail offsets: ({begin_offset}, {end_offset}). Skipping this detail."
                )

        return adjusted_details


class ChatTemplateStrategy(PromptTokenizingStrategy):
    """
    Tokenizing strategy for instruction-based prompts.
    """

    _messages = "messages"

    def __init__(
        self,
        prompter: ChatTemplatePrompter,
        tokenizer,
        train_on_inputs,
        sequence_len,
        roles_to_train=None,
        train_on_eos=None,
    ):
        super().__init__(prompter, tokenizer, train_on_inputs, sequence_len)

        self.roles_to_train = []
        if roles_to_train:
            # map roles if exist in prompter.roles else use the role as is
            self.roles_to_train = [
                prompter.roles.get(role, role) for role in roles_to_train
            ]

        self.train_on_eos = train_on_eos
        self.images = "images"

    @property
    def messages(self):
        return self._messages

    @messages.setter
    def messages(self, messages):
        self._messages = messages

    @property
    def supports_batched(self) -> bool:
        # Let calling code know we can handle lists of examples
        return True

    def is_prompt_batched(self, prompt: dict[str, Any]) -> bool:
        try:
            return all(isinstance(v, list) for v in prompt.values()) and all(
                isinstance(v, list) for v in prompt[self.messages]
            )
        except KeyError:
            return False

    def tokenize_prompt(self, prompt: dict[str, Any]):
        """
        Public method that can handle either a single prompt or a batch of prompts.
        """

        if not self.is_prompt_batched(prompt) or not self.supports_batched:
            return self._tokenize_single_prompt(prompt)

        res = defaultdict(lambda: [])
        feature_names = list(prompt.keys())

        # Process each prompt individually
        for row in zip(*prompt.values()):
            tokenized_prompt = self._tokenize_single_prompt(
                dict(zip(feature_names, row))
            )
            for key, val in tokenized_prompt.items():
                for i in range(0, len(val), self.sequence_len):
                    res[key].append(val[i : i + self.sequence_len])

        # If there are no examples left, return an empty dictionary
        if not res:
            return {}

        return dict(res)

    def _tokenize_single_prompt(self, prompt: dict) -> Dict[str, List[int]]:
        # Old simple legacy behavior that works reliably.
        if (
            not self.roles_to_train
            and not self.train_on_eos
            and not self.prompter.message_field_training  # type: ignore
            and not self.prompter.message_field_training_detail  # type: ignore
        ):
            turns = self.get_conversation_thread(prompt)
            images = self.get_images(prompt)
            prompt_ids = self.prompter.build_prompt(  # type: ignore
                turns[:-1],
                add_generation_prompt=True,
                images=images,
            )
            tokenized_res = self.prompter.build_prompt(turns, images=images)  # type: ignore
            tokenized_prompt = {}
            if isinstance(tokenized_res, list):
                input_ids = prompt_ids + tokenized_res[len(prompt_ids) :]
                tokenized_prompt["input_ids"] = input_ids
                tokenized_prompt["attention_mask"] = [1] * len(input_ids)
            else:
                input_ids = tokenized_res["input_ids"]
                tokenized_prompt = tokenized_res

            if not self.train_on_inputs:
                user_prompt_len = len(prompt_ids)
                labels = [-100] * user_prompt_len + input_ids[user_prompt_len:]
            else:
                labels = input_ids

            tokenized_prompt["labels"] = labels

            return tokenized_prompt

        turns = self.get_conversation_thread(prompt)
        input_ids = self.prompter.build_prompt(turns)  # type: ignore
        labels = [IGNORE_TOKEN_ID] * len(input_ids)

        last_eos_idx = -1
        for index, turn in enumerate(turns):
            role = turn.get("role")
            content = turn.get("content")
            train_turn = turn.get("training")
            train_detail = turn.get("training_detail")

            LOG.debug(
                f"Processing turn {index}: role={role}, content={content}, train_turn={train_turn}, train_detail={train_detail}"
            )

            should_train = None
            if train_turn is not None:
                should_train = train_turn
            elif train_detail is not None:
                should_train = bool(train_detail)
            else:
                should_train = self.train_on_inputs or role in self.roles_to_train

            LOG.debug(f"Should train: {should_train}")

            turn_start_idx, turn_end_idx = self.find_turn(turns=turns, turn_idx=index)

            LOG.debug(f"Turn indices: start={turn_start_idx}, end={turn_end_idx}")

            if should_train and turn_start_idx != -1 and turn_end_idx != -1:
                if train_detail:
                    token_offsets = self.prompter.get_offsets_for_train_detail(  # type: ignore
                        content, train_detail
                    )
                    LOG.debug(f"Token offsets: {token_offsets}")
                    for i, offset in enumerate(token_offsets):
                        if offset != IGNORE_TOKEN_ID and turn_start_idx + i < len(
                            input_ids
                        ):
                            labels[turn_start_idx + i] = input_ids[turn_start_idx + i]
                            LOG.debug(
                                f"Label set at index {turn_start_idx + i}: {input_ids[turn_start_idx + i]}"
                            )
                else:
                    labels[turn_start_idx:turn_end_idx] = input_ids[
                        turn_start_idx:turn_end_idx
                    ]
                    LOG.debug(
                        f"Set labels for training from {turn_start_idx} to {turn_end_idx}"
                    )

                LOG.debug(f"Labels after processing turn {index}: {labels}")

            # Handle EOS token
            eos_idx = self.find_first_eos_token(input_ids, start_idx=turn_end_idx)
            if abs(eos_idx - turn_end_idx) <= 3:  # Allow for some template padding
                last_eos_idx = eos_idx
                if self.train_on_eos == "all" or (
                    self.train_on_eos == "turn" and should_train
                ):
                    labels[eos_idx] = input_ids[eos_idx]
                    LOG.debug(f"EOS token set for training at index {eos_idx}")
            else:
                LOG.debug(
                    f"EOS token missing after turn {turn}. eos_idx: {eos_idx}, turn_end_idx: {turn_end_idx}"
                )

        # Handle 'last' option for train_on_eos
        if self.train_on_eos == "last" and last_eos_idx != -1:
            labels[last_eos_idx] = input_ids[last_eos_idx]
            LOG.debug(f"Last EOS token set for training at index {last_eos_idx}")

        LOG.debug(f"Final labels: {labels}")

        return {
            "input_ids": input_ids,
            "labels": labels,
            "attention_mask": [1] * len(input_ids),
        }

    def find_first_eos_token(self, input_ids, start_idx):
        eos_token_id = self.tokenizer.eos_token_id
        for i in range(start_idx, len(input_ids)):
            if input_ids[i] == eos_token_id:
                return i
        return -1

    def find_turn(self, turns: list[dict], turn_idx: int):
        """
        Locate the starting and ending indices of the specified turn in a conversation.
        """
        # pylint: disable=too-many-return-statements

        if turn_idx >= len(turns):
            raise ValueError(f"Turn index {turn_idx} out of range")

        # mistral does not output message if it contains only system message
        if (
            turn_idx == 0
            and turns[0].get("role") == "system"
            and "mistral" in self.tokenizer.name_or_path.lower()
        ):
            return -1, -1

        empty_turn = {
            "role": turns[turn_idx].get("role"),
            "content": "[[dummy_message]]",
        }

        # Create conversation versions
        turns_with_empty = turns[:turn_idx] + [empty_turn]
        turns_with_content = turns[: turn_idx + 1]

        # Generate the conversation up to the turn, with final turn replaced with dummy content
        dummy_ids = self.prompter.build_prompt(turns_with_empty)  # type: ignore

        # Generate the conversation up to the turn, with final turn included
        full_ids = self.prompter.build_prompt(turns_with_content)  # type: ignore

        if not full_ids or not dummy_ids:
            LOG.warning(f"Empty template generated for turn {turn_idx}")
            return -1, -1

        # Find first difference (start of content)
        start_idx = None
        min_len = min(len(dummy_ids), len(full_ids))
        for i in range(min_len):
            if dummy_ids[i] != full_ids[i]:
                start_idx = i
                break

        if start_idx is None:
            LOG.warning(f"Could not find content start boundary for turn {turn_idx}")
            return -1, -1

        # Find last difference (end of content)
        end_idx = None
        for i in range(min_len):
            dummy_pos = len(dummy_ids) - 1 - i
            full_pos = len(full_ids) - 1 - i
            if dummy_ids[dummy_pos] != full_ids[full_pos]:
                end_idx = full_pos + 1  # Add one to include the last token when slice
                break

        if end_idx is None:
            LOG.warning(f"Could not find content end boundary for turn {turn_idx}")
            return -1, -1

        if end_idx < start_idx:
            LOG.warning(
                f"Content end boundary is before start boundary for turn {turn_idx}"
            )
            return -1, -1

        if end_idx == start_idx:
            LOG.warning(
                f"Content end boundary is the same as start boundary for turn {turn_idx}. This is likely an empty turn."
            )
            return -1, -1

        LOG.debug(f"Content boundaries: {start_idx}, {end_idx}")
        LOG.debug(
            f"Content tokens: {self.tokenizer.convert_ids_to_tokens(full_ids[start_idx:end_idx])}"
        )

        return start_idx, end_idx

    def get_conversation_thread(self, prompt):
        turns = []
        optional_keys = [
            "tool_calls",  # tool that 'assistant' calls
            "name",  # name of tool given by 'tool'
            "tool_call_id",  # mistral/mixtral requires this
        ]
        for message in prompt[self.messages]:
            turn = {
                "role": self.prompter.roles[message[self.prompter.message_field_role]],
                "training": message.get(self.prompter.message_field_training),
                "training_detail": message.get(
                    self.prompter.message_field_training_detail
                ),
            }

            # do not add content if None as it may conflict with some templates due to tools
            content = message.get(self.prompter.message_field_content, None)
            if content is not None:
                turn["content"] = content

            for key in optional_keys:
                value = message.get(key, None)
                if value is not None:
                    turn[key] = value

            turns.append(turn)

        if self.prompter.drop_system_message and turns[0]["role"] == "system":
            turns = turns[1:]

        return turns

    def get_images(self, prompt):
        return prompt.get(self.images, None)


class StrategyLoader:
    """
    Load chat template strategy based on configuration.
    """

    def _get_strategy_cls(self):
        return ChatTemplateStrategy

    def _get_strategy_params(self, cfg, ds_cfg: Dict[str, Any]):
        return {
            "train_on_inputs": cfg.train_on_inputs,
            "sequence_len": cfg.sequence_len,
            "roles_to_train": ds_cfg.get("roles_to_train", ["assistant"]),
            "train_on_eos": ds_cfg.get("train_on_eos", "turn"),
        }

    def __call__(
        self, tokenizer, cfg, ds_cfg: Optional[Dict[str, Any]] = None, processor=None
    ):
        # pylint: disable=duplicate-code
        ds_cfg = ds_cfg or {}
        chat_template_string = get_chat_template_from_config(
            cfg=cfg, ds_cfg=ds_cfg, tokenizer=tokenizer
        )
        LOG.info(f"Using chat template:\n---\n{chat_template_string!s}\n---")

        prompter_params = {
            "tokenizer": tokenizer,
            "chat_template": chat_template_string,
            "message_field_role": ds_cfg.get("message_field_role", "role"),
            "message_field_content": ds_cfg.get("message_field_content", "content"),
            "message_field_training": ds_cfg.get("message_field_training", None),
            "message_field_training_detail": ds_cfg.get(
                "message_field_training_detail",
                None,
            ),
            "roles": ds_cfg.get("roles"),
            "drop_system_message": ds_cfg.get("drop_system_message", False),
            # we need to add one for detecting sequences with exceeding the `sequence_len` limit.
            "max_length": cfg.sequence_len + 1,
            "processor": processor,
        }

        strategy_params = self._get_strategy_params(cfg, ds_cfg)
        strategy_cls = self._get_strategy_cls()

        strategy = strategy_cls(
            ChatTemplatePrompter(**prompter_params),
            tokenizer=tokenizer,
            **strategy_params,
        )

        if "field_messages" in ds_cfg and hasattr(strategy, "messages"):
            strategy.messages = ds_cfg["field_messages"]

        return strategy


load = StrategyLoader()
