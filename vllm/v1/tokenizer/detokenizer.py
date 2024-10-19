import multiprocessing
from dataclasses import dataclass
from typing import Dict, List

import msgspec
import zmq
from msgspec import msgpack

from vllm.transformers_utils.detokenizer_utils import (
    convert_prompt_ids_to_tokens, detokenize_incrementally)
from vllm.transformers_utils.tokenizer import get_tokenizer


class DetokenizerInputs(msgspec.Struct):

    # [num_reqs]
    req_ids: List[str]
    # A request's prompt token ids is sent to the detokenizer only when
    # the request is first detokenized. Otherwise, an empty list is sent.
    prompt_token_ids: List[List[int]]
    new_token_ids: List[List[int]]
    skip_special_tokens: List[bool]
    spaces_between_special_tokens: List[bool]

    # [num_free_reqs]
    free_req_ids: List[str]


class DetokenizerOutputs(msgspec.Struct):

    # [num_reqs]
    req_ids: List[str]
    detokenized_texts: List[str]
    # NOTE(woosuk): The number of the output token ids of each request
    # at the time of detokenization. The detokenizer returns this to the engine
    # because the request state (including the output token ids) is
    # asynchronously updated in the engine, while RequestOutput requires the
    # output token ids to be consistent with the detokenized text.
    num_output_token_ids: List[int]


@dataclass
class RequestState:

    req_id: str

    token_ids: List[int]
    tokens: List[str]
    num_prompt_tokens: int

    prefix_offset: int
    read_offset: int

    skip_special_tokens: bool
    spaces_between_special_tokens: bool

    output_text: str = ""


class Detokenizer(multiprocessing.Process):

    def __init__(
        self,
        tokenizer_name: str,
        pull_port: int,
        push_port: int,
    ):
        super().__init__()
        # NOTE: The pull_port of the detokenizer should be the same as the
        # push_port of the engine. Vice versa.
        self.pull_port = pull_port
        self.push_port = push_port
        self.msgpack_encoder = msgpack.Encoder()
        self.msgpack_decoder = msgpack.Decoder(DetokenizerInputs)

        self.tokenizer = get_tokenizer(tokenizer_name)
        self.requests: Dict[str, RequestState] = {}

    def run(self):
        self.zmq_context = zmq.Context()
        self.pull_socket = self.zmq_context.socket(zmq.PULL)
        self.pull_socket.bind(f"tcp://*:{self.pull_port}")
        self.push_socket = self.zmq_context.socket(zmq.PUSH)
        self.push_socket.bind(f"tcp://*:{self.push_port}")

        while True:
            message = self.pull_socket.recv()
            if message == b"":
                # Terminate signal.
                break
            inputs = self.msgpack_decoder.decode(message)

            for req_id in inputs.free_req_ids:
                self.free(req_id)

            detokenized_texts: List[str] = []
            num_output_token_ids: List[int] = []
            num_reqs = len(inputs.req_ids)
            for i in range(num_reqs):
                req_id = inputs.req_ids[i]
                if req_id not in self.requests:
                    self.add_request(
                        request_id=req_id,
                        prompt_token_ids=inputs.prompt_token_ids[i],
                        skip_special_tokens=inputs.skip_special_tokens[i],
                        spaces_between_special_tokens=inputs.
                        spaces_between_special_tokens[i],
                    )
                new_str = self.detokenize(req_id, inputs.new_token_ids[i])
                detokenized_texts.append(new_str)
                req_state = self.requests[req_id]
                num_output_token_ids.append(
                    len(req_state.token_ids) - req_state.num_prompt_tokens)

            detokenized = DetokenizerOutputs(
                req_ids=inputs.req_ids,
                detokenized_texts=detokenized_texts,
                num_output_token_ids=num_output_token_ids,
            )
            self.push_socket.send(self.msgpack_encoder.encode(detokenized),
                                  flags=zmq.NOBLOCK)

    def add_request(
        self,
        request_id: str,
        prompt_token_ids: List[int],
        skip_special_tokens: bool,
        spaces_between_special_tokens: bool,
    ) -> None:
        tokens, prefix_offset, read_offset = convert_prompt_ids_to_tokens(
            tokenizer=self.tokenizer,
            prompt_ids=prompt_token_ids,
            skip_special_tokens=skip_special_tokens,
        )
        self.requests[request_id] = RequestState(
            req_id=request_id,
            token_ids=prompt_token_ids,
            tokens=tokens,
            num_prompt_tokens=len(prompt_token_ids),
            prefix_offset=prefix_offset,
            read_offset=read_offset,
            skip_special_tokens=skip_special_tokens,
            spaces_between_special_tokens=spaces_between_special_tokens,
        )

    def free(self, request_id: str) -> None:
        del self.requests[request_id]

    def detokenize(self, request_id: str, new_token_ids: List[int]) -> str:
        # TODO(woosuk): This method becomes very inefficient when the number of
        # new_token_ids is more than 1. We need to optimize this.
        req_state = self.requests[request_id]
        decoded_text = ""
        for new_token_id in new_token_ids:
            req_state.token_ids.append(new_token_id)
            (new_tokens, new_decoded_token_text, prefix_offset,
             read_offset) = detokenize_incrementally(
                 tokenizer=self.tokenizer,
                 all_input_ids=req_state.token_ids,
                 prev_tokens=req_state.tokens,
                 prefix_offset=req_state.prefix_offset,
                 read_offset=req_state.read_offset,
                 skip_special_tokens=req_state.skip_special_tokens,
                 spaces_between_special_tokens=req_state.
                 spaces_between_special_tokens,
             )

            req_state.tokens.extend(new_tokens)
            req_state.prefix_offset = prefix_offset
            req_state.read_offset = read_offset
            req_state.output_text += new_decoded_token_text
            decoded_text += new_decoded_token_text
        return decoded_text