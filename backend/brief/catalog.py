from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BriefSource:
    id: str
    name: str
    url: str
    kind: str
    format: str = "rss"
    article_prefix: str = ""


SOURCES = (
    BriefSource("arxiv", "arXiv", "https://rss.arxiv.org/rss/cs.CL+cs.LG+cs.AI+stat.ML+cs.DC+cs.MA", "paper"),
    BriefSource("openai", "OpenAI", "https://openai.com/news/rss.xml", "lab"),
    BriefSource("anthropic", "Anthropic Research", "https://www.anthropic.com/research", "lab", "html", "/research/"),
    BriefSource("deepmind", "Google DeepMind", "https://deepmind.google/blog/rss.xml", "lab"),
    BriefSource("google", "Google Research", "https://research.google/blog/rss/", "lab"),
    BriefSource("meta", "Meta AI", "https://ai.meta.com/blog/", "lab", "html", "/blog/"),
    BriefSource("microsoft", "Microsoft Research", "https://www.microsoft.com/en-us/research/feed/", "lab"),
    BriefSource("qwen", "Qwen", "https://qwenlm.github.io/", "lab", "html", "/blog/"),
    BriefSource("deepseek", "DeepSeek", "https://api-docs.deepseek.com/news/", "lab", "html", "/news/"),
    BriefSource("mistral", "Mistral", "https://mistral.ai/news", "lab", "html", "/news/"),
    BriefSource("allenai", "Allen Institute for AI", "https://allenai.org/blog", "lab", "html", "/blog/"),
    BriefSource("pytorch", "PyTorch releases", "https://api.github.com/repos/pytorch/pytorch/releases/latest", "engineering", "github-release"),
    BriefSource("triton", "Triton releases", "https://api.github.com/repos/triton-lang/triton/releases/latest", "engineering", "github-release"),
    BriefSource("vllm", "vLLM releases", "https://github.com/vllm-project/vllm/releases.atom", "engineering", "atom"),
    BriefSource("transformers", "Transformers releases", "https://github.com/huggingface/transformers/releases.atom", "engineering", "atom"),
    BriefSource("huggingface", "Hugging Face technical blog", "https://huggingface.co/blog/feed.xml", "reading"),
)

TOPICS = {
    "training": ("LLM training", ("pretraining", "pre-training", "fine-tuning", "finetuning", "training", "sft", "data mixture", "distillation", "scaling law", "scaling laws")),
    "inference": ("Inference efficiency", ("inference", "quantization", "speculative decoding", "decoding", "kv cache", "kv-cache", "serving", "kernel", "kernels", "batching")),
    "test-time": ("Test-time compute", ("test-time", "test time", "reasoning", "verifier", "adaptive compute", "inference-time", "process reward")),
    "rl": ("RL & post-training", ("reinforcement learning", "rl", "rlhf", "rlvr", "rlaif", "grpo", "ppo", "dpo", "reward", "rewards", "preference", "post-training", "post training", "credit assignment")),
    "context": ("Context & memory", ("long-context", "context", "context window", "context management", "context compression", "retrieval", "memory", "positional")),
    "agents": ("Agents & tool use", ("agent", "agents", "agentic", "tool use", "tool-use", "tool learning", "tool calling", "function calling", "planning")),
    "foundations": ("ML / DL foundations", ("optimization", "optimizer", "optimizers", "generalization", "representation learning", "learning theory", "learning dynamics", "architecture", "architectures", "transformer", "transformers", "state space", "mamba", "mixture of experts", "moe", "attention", "gradient", "gradients", "neural", "deep learning", "machine learning")),
}
