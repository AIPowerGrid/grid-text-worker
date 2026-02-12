# vLLM Optimization Guide

Advanced optimization techniques for maximizing throughput, reducing latency, and improving efficiency with vLLM on the Grid Inference Worker.

## Table of Contents

1. [Performance Metrics](#performance-metrics)
2. [GPU Optimization](#gpu-optimization)
3. [Memory Management](#memory-management)
4. [Model Optimization](#model-optimization)
5. [Batching Strategies](#batching-strategies)
6. [Multi-GPU Setup](#multi-gpu-setup)
7. [Benchmarking](#benchmarking)
8. [Production Best Practices](#production-best-practices)

## Performance Metrics

### Key Metrics to Monitor

| Metric | Target | Description |
|--------|--------|-------------|
| **Throughput** | >20 tok/s | Tokens generated per second per request |
| **Latency (TTFT)** | <500ms | Time to first token |
| **Requests/sec** | Varies | Concurrent requests processed |
| **GPU Utilization** | >80% | GPU compute usage |
| **VRAM Usage** | <90% | GPU memory consumption |
| **KV Cache Hit Rate** | >70% | Prefix caching efficiency |

### Measuring Performance

```bash
# Check GPU stats
nvidia-smi -l 1

# Monitor vLLM metrics (Prometheus endpoint)
curl http://127.0.0.1:8000/metrics

# Test generation speed
time curl -X POST http://127.0.0.1:8000/v1/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama2-7b-chat",
    "prompt": "Write a story about AI",
    "max_tokens": 100
  }'
```

## GPU Optimization

### 1. GPU Memory Utilization

**Balance memory allocation between model weights and KV cache:**

```bash
# Conservative (more memory for KV cache, better for long contexts)
--gpu-memory-utilization 0.85

# Aggressive (more requests in parallel, risk of OOM)
--gpu-memory-utilization 0.95

# Recommended starting point
--gpu-memory-utilization 0.90
```

**Rule of thumb:**
- More available VRAM → increase utilization (0.95)
- Long contexts (>4K) → decrease utilization (0.85)
- Many concurrent requests → decrease utilization (0.85)

### 2. Enable Flash Attention

Flash Attention is **automatically enabled** in vLLM for supported models. Verify:

```bash
# Check vLLM logs for:
INFO: Using Flash Attention backend
```

Benefits:
- 2-4x faster attention computation
- Reduced memory usage
- No accuracy loss

### 3. CUDA Graphs

vLLM uses CUDA graphs by default for optimized execution. To disable (debugging only):

```bash
--disable-cuda-graph
```

Keep enabled for production (default behavior).

### 4. Data Type Selection

Choose optimal dtype for your GPU:

```bash
# Auto-detect (recommended)
--dtype auto

# Force FP16 (A100, A6000, RTX 3090+)
--dtype float16

# Force BF16 (A100, H100, best precision)
--dtype bfloat16

# FP32 (debugging only, very slow)
--dtype float32
```

**GPU Recommendations:**
- **A100/H100**: Use `bfloat16`
- **RTX 3090/4090**: Use `float16`
- **T4**: Use `float16`
- **V100**: Use `float16`

## Memory Management

### 1. KV Cache Configuration

KV cache stores attention keys/values for generated tokens. Optimize with:

```bash
# Maximum number of tokens in KV cache
--max-num-seqs 256          # Default, adjust based on workload

# Batch size for continuous batching
--max-num-batched-tokens 8192  # Default

# Enable prefix caching (shares common prompts)
--enable-prefix-caching
```

**Prefix Caching** is crucial for Grid Worker because:
- System prompts are reused across requests
- Up to 70% cache hit rate in production
- **Always enable** unless memory constrained

### 2. Context Length Optimization

Longer contexts = more memory. Set appropriately:

```bash
# Match model's trained context or less
--max-model-len 4096   # For 4K context models

# Reduce for memory savings
--max-model-len 2048   # If most requests are <2K tokens

# Extend (if model supports)
--max-model-len 8192   # For Mistral 7B or longer-context models
```

Update Grid Worker `.env` to match:
```bash
GRID_MAX_CONTEXT_LENGTH=4096  # Must match vLLM --max-model-len
```

### 3. Block Size Tuning

vLLM divides KV cache into blocks. Tune for your workload:

```bash
# Default (good for most cases)
--block-size 16

# Smaller blocks (better memory utilization, slight overhead)
--block-size 8

# Larger blocks (less overhead, potential waste)
--block-size 32
```

**Recommendation**: Start with default (16), only change if profiling shows benefit.

## Model Optimization

### 1. Quantization

Reduce model size and increase speed with quantization:

#### AWQ (4-bit, recommended)

```bash
python -m vllm.entrypoints.openai.api_server \
  --model TheBloke/Llama-2-7B-Chat-AWQ \
  --quantization awq \
  --dtype half \
  --max-model-len 4096
```

**Benefits:**
- 4x less VRAM (7B model: 16GB → 4GB)
- 1.5-2x faster inference
- Minimal accuracy loss (<2%)

#### GPTQ (4-bit, alternative)

```bash
python -m vllm.entrypoints.openai.api_server \
  --model TheBloke/Llama-2-7B-Chat-GPTQ \
  --quantization gptq
```

**Comparison:**
- **AWQ**: Faster, better accuracy
- **GPTQ**: Wider model support

#### SqueezeLLM (more aggressive)

```bash
--quantization squeezellm
```

**Use when:** Extreme memory constraints (not recommended for production)

### 2. Model Selection Best Practices

**For Grid Worker earnings optimization:**

| Goal | Recommended Model | VRAM | Expected Performance |
|------|-------------------|------|---------------------|
| **Max throughput** | Llama-3.2-3B-AWQ | 4GB | 40-60 tok/s |
| **Balanced** | Llama-2-7B-AWQ | 8GB | 25-35 tok/s |
| **Quality** | Mistral-7B-v0.3 | 16GB | 20-30 tok/s |
| **Premium** | Llama-2-13B-AWQ | 16GB | 15-25 tok/s |

Higher tok/s → more jobs/hour → more kudos

### 3. Trust Remote Code

For models with custom code:

```bash
--trust-remote-code
```

**Required for:** Phi, Qwen, InternLM, and some fine-tuned models

## Batching Strategies

### 1. Continuous Batching

vLLM automatically batches requests. Optimize with:

```bash
# Max sequences processed together
--max-num-seqs 256

# Higher = more throughput, more memory
--max-num-seqs 512  # If you have VRAM headroom

# Lower = less memory, lower throughput
--max-num-seqs 128  # If OOM errors occur
```

### 2. Dynamic Batching Parameters

```bash
# Maximum tokens processed in one iteration
--max-num-batched-tokens 8192

# Increase for higher throughput (if memory allows)
--max-num-batched-tokens 16384

# Scheduling policy
--scheduling-policy fcfs  # First-come-first-serve (default)
# --scheduling-policy priority  # Priority-based (future)
```

### 3. Grid Worker Threading

Coordinate with vLLM batching:

**`.env` configuration:**
```bash
# Single thread = vLLM handles all batching
GRID_MAX_THREADS=1  # Recommended for most setups

# Multiple threads = useful if vLLM has spare capacity
GRID_MAX_THREADS=2  # Only if GPU util < 80%
```

**Recommendation:** Start with `GRID_MAX_THREADS=1`, increase only if GPU utilization is low.

## Multi-GPU Setup

### 1. Tensor Parallelism

Split model across multiple GPUs (same node):

```bash
# Use 2 GPUs
python -m vllm.entrypoints.openai.api_server \
  --model meta-llama/Llama-2-70B-chat-hf \
  --tensor-parallel-size 2 \
  --gpu-memory-utilization 0.95

# Use 4 GPUs
--tensor-parallel-size 4
```

**When to use:**
- Model doesn't fit on single GPU (70B+ models)
- Want faster inference with large models

**Requirements:**
- GPUs on same node
- GPUs must be identical
- NVLink recommended (not required)

### 2. Pipeline Parallelism

Split model layers across GPUs:

```bash
--pipeline-parallel-size 2
```

**When to use:**
- Very large models (70B+)
- Combine with tensor parallelism for huge models

**Example: 70B on 4 GPUs**
```bash
--tensor-parallel-size 2 \
--pipeline-parallel-size 2
```

### 3. Multi-Instance (Advanced)

Run multiple vLLM instances for load balancing:

```bash
# Instance 1 on GPU 0
CUDA_VISIBLE_DEVICES=0 python -m vllm.entrypoints.openai.api_server \
  --model meta-llama/Llama-2-7B-chat-hf \
  --port 8000

# Instance 2 on GPU 1
CUDA_VISIBLE_DEVICES=1 python -m vllm.entrypoints.openai.api_server \
  --model meta-llama/Llama-2-7B-chat-hf \
  --port 8001
```

Use nginx or HAProxy for load balancing.

## Benchmarking

### 1. Built-in Benchmark

```bash
# Install benchmark tool
pip install vllm

# Run benchmark
python -m vllm.benchmarks.benchmark \
  --model meta-llama/Llama-2-7B-chat-hf \
  --dataset-name sharegpt \
  --num-prompts 100
```

### 2. Custom Load Testing

**Test script `benchmark.py`:**
```python
import asyncio
import time
from openai import AsyncOpenAI

client = AsyncOpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="not-needed"
)

async def single_request():
    start = time.time()
    response = await client.chat.completions.create(
        model="llama2-7b-chat",
        messages=[{"role": "user", "content": "Write a short story"}],
        max_tokens=100
    )
    latency = time.time() - start
    return latency, len(response.choices[0].message.content)

async def benchmark(num_requests=50, concurrency=10):
    start = time.time()

    # Run concurrent requests
    tasks = [single_request() for _ in range(num_requests)]
    results = await asyncio.gather(*tasks)

    total_time = time.time() - start
    latencies = [r[0] for r in results]
    tokens = sum(r[1] for r in results)

    print(f"Requests: {num_requests}")
    print(f"Concurrency: {concurrency}")
    print(f"Total time: {total_time:.2f}s")
    print(f"Throughput: {num_requests/total_time:.2f} req/s")
    print(f"Tokens/sec: {tokens/total_time:.2f}")
    print(f"Avg latency: {sum(latencies)/len(latencies):.2f}s")
    print(f"P50 latency: {sorted(latencies)[len(latencies)//2]:.2f}s")
    print(f"P99 latency: {sorted(latencies)[int(len(latencies)*0.99)]:.2f}s")

asyncio.run(benchmark())
```

Run:
```bash
python benchmark.py
```

### 3. Monitor Grid Worker Stats

Check dashboard at `http://localhost:7861` or via API:

```bash
curl http://localhost:7861/api/stats
```

Key metrics:
- `kudos_per_hour`: Earnings rate
- `jobs_per_hour`: Throughput
- `jobs_completed`: Total processed
- `jobs_failed`: Error rate (should be <1%)

## Production Best Practices

### 1. Startup Configuration

**Production-ready vLLM startup:**

```bash
#!/bin/bash
# start_vllm_production.sh

MODEL="TheBloke/Llama-2-7B-Chat-AWQ"
NAME="llama2-7b-awq"

python -m vllm.entrypoints.openai.api_server \
  --model $MODEL \
  --served-model-name $NAME \
  --host 0.0.0.0 \
  --port 8000 \
  --gpu-memory-utilization 0.90 \
  --max-model-len 4096 \
  --max-num-seqs 256 \
  --dtype auto \
  --enable-prefix-caching \
  --trust-remote-code \
  --tensor-parallel-size 1 \
  2>&1 | tee vllm.log
```

### 2. Systemd Service (Linux)

**`/etc/systemd/system/vllm.service`:**
```ini
[Unit]
Description=vLLM Inference Server
After=network.target

[Service]
Type=simple
User=your-user
WorkingDirectory=/home/your-user
Environment="CUDA_VISIBLE_DEVICES=0"
ExecStart=/usr/bin/python3 -m vllm.entrypoints.openai.api_server \
  --model TheBloke/Llama-2-7B-Chat-AWQ \
  --served-model-name llama2-7b-awq \
  --host 0.0.0.0 \
  --port 8000 \
  --gpu-memory-utilization 0.90 \
  --max-model-len 4096 \
  --enable-prefix-caching
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable:
```bash
sudo systemctl daemon-reload
sudo systemctl enable vllm
sudo systemctl start vllm
sudo systemctl status vllm
```

### 3. Docker Compose

**`docker-compose.vllm.yml`:**
```yaml
version: '3.8'

services:
  vllm:
    image: vllm/vllm-openai:latest
    runtime: nvidia
    ports:
      - "8000:8000"
    volumes:
      - ~/.cache/huggingface:/root/.cache/huggingface
    environment:
      - CUDA_VISIBLE_DEVICES=0
    command: >
      --model TheBloke/Llama-2-7B-Chat-AWQ
      --served-model-name llama2-7b-awq
      --host 0.0.0.0
      --port 8000
      --gpu-memory-utilization 0.90
      --max-model-len 4096
      --enable-prefix-caching
    restart: unless-stopped
    shm_size: '16gb'
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

Start:
```bash
docker compose -f docker-compose.vllm.yml up -d
```

### 4. Monitoring Setup

**Prometheus metrics (vLLM exposes automatically):**
```bash
curl http://127.0.0.1:8000/metrics
```

Key metrics:
- `vllm:num_requests_running`: Active requests
- `vllm:gpu_cache_usage_perc`: KV cache utilization
- `vllm:time_to_first_token`: TTFT latency
- `vllm:time_per_output_token`: Generation speed

### 5. Logging

```bash
# Enable detailed logging
export VLLM_LOGGING_LEVEL=DEBUG

# Log to file
python -m vllm.entrypoints.openai.api_server \
  --model ... \
  2>&1 | tee vllm_$(date +%Y%m%d).log
```

### 6. Health Checks

**Check vLLM health:**
```bash
# Models endpoint
curl http://127.0.0.1:8000/v1/models

# Version endpoint
curl http://127.0.0.1:8000/version

# Simple generation test
curl -X POST http://127.0.0.1:8000/v1/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama2-7b-awq",
    "prompt": "Hello",
    "max_tokens": 5
  }'
```

### 7. Optimization Checklist

Before going to production:

- [ ] Quantized model (AWQ/GPTQ) for 4x memory savings
- [ ] `--enable-prefix-caching` enabled
- [ ] `--gpu-memory-utilization` tuned (0.85-0.95)
- [ ] `--max-model-len` matches actual usage
- [ ] Flash Attention verified in logs
- [ ] `GRID_MAX_CONTEXT_LENGTH` matches vLLM config
- [ ] Health checks passing
- [ ] Benchmark shows >20 tok/s per request
- [ ] GPU utilization >80%
- [ ] Systemd/Docker service configured
- [ ] Logs monitored for OOM errors

## Common Optimization Scenarios

### Scenario 1: Maximize Throughput (Jobs/Hour)

**Goal:** Process as many Grid jobs as possible

**Configuration:**
```bash
# Use fastest quantized model
--model TheBloke/Llama-2-7B-Chat-AWQ
--quantization awq

# Aggressive batching
--max-num-seqs 512
--max-num-batched-tokens 16384

# High memory utilization
--gpu-memory-utilization 0.95

# Enable caching
--enable-prefix-caching
```

**Expected:** 30-50 jobs/hour on single RTX 3090

### Scenario 2: Optimize for Long Context

**Goal:** Handle 8K+ token contexts efficiently

**Configuration:**
```bash
# Extended context
--max-model-len 8192

# More memory for KV cache
--gpu-memory-utilization 0.85

# Fewer concurrent sequences
--max-num-seqs 128

# Prefix caching crucial for long contexts
--enable-prefix-caching
```

### Scenario 3: Multi-Model Setup

**Goal:** Serve multiple models on same GPU

**Not recommended.** Instead:
1. Use vLLM's model swapping (future feature)
2. Or run separate Grid Workers per model

### Scenario 4: Cost Optimization

**Goal:** Minimize GPU cost per kudos

**Use smallest capable model:**
```bash
# Llama-3.2-3B-AWQ can run on 8GB GPU
--model shuyuej/Llama-3.2-3B-Instruct-AWQ
--gpu-memory-utilization 0.90
--max-num-seqs 512
```

**Measure efficiency:**
- Kudos per GPU hour = `kudos_per_hour` (from dashboard)
- Cost efficiency = `kudos_per_hour / GPU_hourly_cost`

## Troubleshooting Performance

### Slow Generation (<10 tok/s)

1. Check GPU utilization: `nvidia-smi`
   - Low (<50%): Increase `--max-num-seqs`
   - High (>95%): Bottlenecked, working as fast as possible

2. Check VRAM usage
   - Near limit: Reduce `--max-model-len` or use quantized model
   - Lots of free space: Increase `--max-num-seqs`

3. Verify Flash Attention is enabled (check logs)

4. Try quantized model (AWQ) for 1.5-2x speedup

### OOM (Out of Memory) Errors

1. Reduce `--gpu-memory-utilization` to 0.80
2. Reduce `--max-model-len`
3. Reduce `--max-num-seqs`
4. Use quantized model (4-bit AWQ)
5. Disable prefix caching (last resort)

### High Latency (>2s TTFT)

1. Check model size vs GPU (7B on RTX 3090 should be <500ms)
2. Verify no CPU offloading (check logs for warnings)
3. Reduce `--max-num-seqs` to prioritize latency over throughput
4. Use smaller model or quantized version

## Next Steps

- [vLLM Setup Guide](vllm-setup-guide.md) - Installation and basic configuration
- [Grid Worker README](../README.md) - General worker setup
- [vLLM GitHub](https://github.com/vllm-project/vllm) - Latest features and updates

## Resources

- **vLLM Performance Tuning**: https://docs.vllm.ai/en/latest/performance.html
- **Model Quantization**: https://huggingface.co/docs/transformers/main/quantization
- **CUDA Best Practices**: https://docs.nvidia.com/cuda/cuda-c-best-practices-guide/
- **AI Power Grid Dashboard**: https://aipowergrid.io
