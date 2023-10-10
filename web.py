# Copyright (c) Meta Platforms, Inc. and affiliates.
# This software may be used and distributed according to the terms of the GNU General Public License version 3.

from typing import Tuple
import os
import sys
import argparse
import torch
import time
import json

from pathlib import Path
from typing import List

from pydantic import BaseModel
from fastapi import FastAPI, Request, WebSocket
import uvicorn
import torch.distributed as dist
import asyncio
from sse_starlette.sse import EventSourceResponse

from fairscale.nn.model_parallel.initialize import initialize_model_parallel

from llama import ModelArgs, Transformer, Tokenizer, LLaMA


parser = argparse.ArgumentParser()
parser.add_argument('--ckpt_dir', type=str, required=True)
parser.add_argument('--tokenizer_path', type=str, required=True)
parser.add_argument('--max_seq_len', type=int, default=2048)
parser.add_argument('--max_batch_size', type=int, default=1)


app = FastAPI()


def setup_model_parallel() -> Tuple[int, int]:
    local_rank = int(os.environ.get("LOCAL_RANK", -1))
    world_size = int(os.environ.get("WORLD_SIZE", -1))

    dist.init_process_group("nccl")
    initialize_model_parallel(world_size)
    torch.cuda.set_device(local_rank)

    # seed must be the same in all processes
    torch.manual_seed(1)
    return local_rank, world_size


def load(
    ckpt_dir: str,
    tokenizer_path: str,
    local_rank: int,
    world_size: int,
    max_seq_len: int,
    max_batch_size: int,
) -> LLaMA:
    start_time = time.time()
    checkpoints = sorted(Path(ckpt_dir).glob("*.pth"))
    assert world_size == len(
        checkpoints
    ), f"Loading a checkpoint for MP={len(checkpoints)} but world size is {world_size}"
    ckpt_path = checkpoints[local_rank]
    print("Loading")
    checkpoint = torch.load(ckpt_path, map_location="cpu")
    with open(Path(ckpt_dir) / "params.json", "r") as f:
        params = json.loads(f.read())

    model_args: ModelArgs = ModelArgs(
        max_seq_len=max_seq_len, max_batch_size=max_batch_size, **params
    )
    tokenizer = Tokenizer(model_path=tokenizer_path)
    model_args.vocab_size = tokenizer.n_words
    torch.set_default_tensor_type(torch.cuda.HalfTensor)
    model = Transformer(model_args)
    torch.set_default_tensor_type(torch.FloatTensor)
    model.load_state_dict(checkpoint, strict=False)

    generator = LLaMA(model, tokenizer)
    print(f"Loaded in {time.time() - start_time:.2f} seconds")
    return generator


def init_generator(
    ckpt_dir: str,
    tokenizer_path: str,
    max_seq_len: int = 8192,
    max_batch_size: int = 32,
):
    local_rank, world_size = setup_model_parallel()
    if local_rank > 0:
        sys.stdout = open(os.devnull, "w")

    generator = load(
        ckpt_dir, tokenizer_path, local_rank, world_size, max_seq_len, max_batch_size
    )

    return generator


if __name__ == "__main__":
    args = parser.parse_args()
    generator = init_generator(
        args.ckpt_dir,
        args.tokenizer_path,
        args.max_seq_len,
        args.max_batch_size,
    )

    class Config(BaseModel):
        prompts: List[str]
        max_gen_len: int
        temperature: float = 0.8
        top_p: float = 0.95

    if dist.get_rank() == 0:
        @app.post("/api/v1/generate")
        async def generate(request: Request):
            text = (await request.json())["text"]
            dist.broadcast_object_list([text, 100028, 0.7, 1])
            async def generate_stream():
                print("starting stream, text:", text)
                for token, word, prob, place, top5 in generator.probs_stream(
                text, max_gen_len=100028, temperature=0.7, top_p=1
            ):
                    # print(f"{token} {word} {prob} {place} {top5}")
                    yield {"data": json.dumps({"token": token, "word": word, "probability": prob, "place": place, "top5": top5})}
                    await asyncio.sleep(0)

                yield {"data": "<EOS>"}
                # end of stream

            return EventSourceResponse(generate_stream())
        
        @app.websocket("/api/v1/generate")
        async def generate_ws(websocket: WebSocket):
            await websocket.accept()
            text = await websocket.receive_text()

            dist.broadcast_object_list([text, 100028, 0.7, 1])

            recv_tokens = 0
            send_tokens = 0

            print("starting stream, text:", text)
            for token, word, prob, place, top5 in generator.probs_stream(
            text, max_gen_len=100028, temperature=0.7, top_p=1
        ):
                # print(f"{token} {word} {prob} {place} {top5}")
                await websocket.send_text(json.dumps({"token": token, "word": word, "probability": prob, "place": place, "top5": top5}))

                send_tokens += 1

                while recv_tokens < send_tokens - 10:
                    await websocket.receive_text()
                    recv_tokens += 1

                await asyncio.sleep(0)

            await websocket.close()

        uvicorn.run(app, host="0.0.0.0", port=8042)
    else:
        while True:
            config = [None] * 4
            try:
                dist.broadcast_object_list(config)
                generator.generate(
                    config[0], max_gen_len=config[1], temperature=config[2], top_p=config[3]
                )
            except:
                pass
