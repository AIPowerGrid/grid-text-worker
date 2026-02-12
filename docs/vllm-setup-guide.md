# vLLM Setup Guide for Grid Inference Worker

This guide covers setting up vLLM as a high-performance inference backend for the Grid Inference Worker.

## Table of Contents

1. [What is vLLM?](#what-is-vllm)
2. [Installation](#installation)
3. [Basic Configuration](#basic-configuration)
4. [Integration with Grid Worker](#integration-with-grid-worker)
5. [Model Selection](#model-selection)
6. [Troubleshooting](#troubleshooting)

## What is vLLM?

[vLLM](https://github.com/vllm-project/vllm) is a fast and memory-efficient inference engine optimized for LLM serving. Key advantages:

- **PagedAttention**: Efficient memory management for KV cache
- **Continuous batching**: Process requests as they arrive without waiting for a batch to fill
- **Quantization support**: FP16, INT8, INT4 (AWQ, GPTQ, SqueezeLLM)
- **High throughput**: Up to 24x faster than baseline HuggingFace implementations
- **OpenAI-compatible API**: Drop-in replacement for OpenAI API

## Installation

### Prerequisites

- **Python**: 3.9 or newer
- **CUDA**: 11.8 or newer (for NVIDIA GPUs)
- **GPU VRAM**: Minimum 8GB (model-dependent)
- **OS**: Linux (recommended), Windows WSL2, macOS (CPU-only)

### Install vLLM

#### Option 1: pip (Recommended)

```bash
# Install vLLM with CUDA support
pip install vllm

# For specific CUDA version (e.g., CUDA 11.8)
pip install vllm-cuda118

# For CUDA 12.1
pip install vllm
```

#### Option 2: Docker

```bash
# Pull official vLLM image
docker pull vllm/vllm-openai:latest

# Run vLLM server
docker run --runtime nvidia --gpus all \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -p 8000:8000 \
  --ipc=host \
  vllm/vllm-openai:latest \
  --model meta-llama/Llama-2-7b-chat-hf
```

#### Option 3: Build from Source

```bash
git clone https://github.com/vllm-project/vllm.git
cd vllm
pip install -e .
```

### Verify Installation

```bash
python -c "import vllm; print(vllm.__version__)"
```

## Basic Configuration

### Starting vLLM Server

#### Simple Start

```bash
# Start vLLM with a model
python -m vllm.entrypoints.openai.api_server \
  --model meta-llama/Llama-2-7b-chat-hf \
  --served-model-name llama2-7b-chat \
  --port 8000
```

#### With Custom Settings

```bash
python -m vllm.entrypoints.openai.api_server \
  --model meta-llama/Llama-2-7b-chat-hf \
  --served-model-name llama2-7b-chat \
  --host 0.0.0.0 \
  --port 8000 \
  --gpu-memory-utilization 0.9 \
  --max-model-len 4096 \
  --tensor-parallel-size 1
```

#### Key Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `--model` | HuggingFace model path or local path | Required |
| `--served-model-name` | Name used by clients (must match MODEL_NAME in .env) | Same as model |
| `--port` | Server port | 8000 |
| `--host` | Server host | 0.0.0.0 |
| `--gpu-memory-utilization` | GPU memory fraction (0.0-1.0) | 0.9 |
| `--max-model-len` | Maximum context length | Auto-detected |
| `--tensor-parallel-size` | Number of GPUs for tensor parallelism | 1 |
| `--dtype` | Data type (auto, half, float16, bfloat16, float32) | auto |

### Configuration Files

Create a startup script for convenience:

**`start_vllm.sh`**
```bash
#!/bin/bash

MODEL_NAME="meta-llama/Llama-2-7b-chat-hf"
SERVED_NAME="llama2-7b-chat"
PORT=8000
GPU_MEM=0.9
MAX_LEN=4096

python -m vllm.entrypoints.openai.api_server \
  --model $MODEL_NAME \
  --served-model-name $SERVED_NAME \
  --host 0.0.0.0 \
  --port $PORT \
  --gpu-memory-utilization $GPU_MEM \
  --max-model-len $MAX_LEN \
  --trust-remote-code
```

Make it executable:
```bash
chmod +x start_vllm.sh
./start_vllm.sh
```

## Integration with Grid Worker

### Configure Grid Worker for vLLM

1. **Edit `.env` configuration:**

```bash
# --- Required ---
GRID_API_KEY=your-api-key-here
MODEL_NAME=llama2-7b-chat  # Must match --served-model-name

# --- Grid settings ---
GRID_WORKER_NAME=vLLM-Worker
GRID_MAX_LENGTH=4096
GRID_MAX_CONTEXT_LENGTH=4096

# --- Backend: OpenAI-compatible (vLLM) ---
BACKEND_TYPE=openai
OPENAI_URL=http://127.0.0.1:8000/v1
# OPENAI_API_KEY=  # Leave empty for local vLLM
```

2. **Start vLLM first:**

```bash
# In terminal 1: Start vLLM server
python -m vllm.entrypoints.openai.api_server \
  --model meta-llama/Llama-2-7b-chat-hf \
  --served-model-name llama2-7b-chat
```

3. **Start Grid Worker:**

```bash
# In terminal 2: Start grid worker
grid-inference-worker
```

### Verify Connection

The worker will auto-detect vLLM on port 8000. Check the logs:

```
🚀 Worker starting      | 🧠 grid/llama2-7b-chat
📡 Backend              | openai @ http://127.0.0.1:8000/v1/chat/completions
```

## Model Selection

### Popular Models for vLLM

| Model | Size | VRAM | Context | Use Case |
|-------|------|------|---------|----------|
| Llama-3.2-3B | 3B | 8GB | 128K | Small, efficient |
| Llama-2-7B | 7B | 16GB | 4K | General purpose |
| Mistral-7B-v0.3 | 7B | 16GB | 32K | Long context |
| Llama-2-13B | 13B | 28GB | 4K | Better quality |
| Llama-2-70B | 70B | 160GB | 4K | Production-grade |

### Quantized Models

For reduced VRAM requirements, use quantized models:

#### AWQ (Activation-aware Weight Quantization)

```bash
# 4-bit quantized models require ~4x less VRAM
python -m vllm.entrypoints.openai.api_server \
  --model TheBloke/Llama-2-7B-Chat-AWQ \
  --served-model-name llama2-7b-awq \
  --quantization awq \
  --dtype half
```

#### GPTQ

```bash
python -m vllm.entrypoints.openai.api_server \
  --model TheBloke/Llama-2-7B-Chat-GPTQ \
  --served-model-name llama2-7b-gptq \
  --quantization gptq
```

### Model Download Location

Models are cached in:
- **Linux/macOS**: `~/.cache/huggingface/hub/`
- **Windows**: `C:\Users\<username>\.cache\huggingface\hub\`

## Troubleshooting

### vLLM Server Not Starting

**Problem**: `CUDA out of memory` error

**Solution**: Reduce GPU memory utilization
```bash
--gpu-memory-utilization 0.7  # Try 0.7 instead of 0.9
```

**Problem**: `ModuleNotFoundError: No module named 'vllm'`

**Solution**: Install vLLM in correct environment
```bash
pip install vllm
```

### Grid Worker Connection Issues

**Problem**: Worker shows "Backend connection error"

**Solution**: Verify vLLM is running
```bash
curl http://127.0.0.1:8000/v1/models
```

**Problem**: "Model not found" error

**Solution**: Ensure `MODEL_NAME` in `.env` matches `--served-model-name`:
```bash
# In vLLM start command
--served-model-name llama2-7b-chat

# In .env
MODEL_NAME=llama2-7b-chat
```

### Performance Issues

**Problem**: Slow generation speed (<5 tokens/sec)

**Solutions**:
1. Check GPU utilization: `nvidia-smi`
2. Reduce max context length: `--max-model-len 2048`
3. Enable Flash Attention: Already enabled by default in vLLM
4. See [vLLM Optimization Guide](vllm-optimization-guide.md)

### Model Loading Errors

**Problem**: `Failed to load model weights`

**Solution**: Check disk space and HuggingFace token
```bash
# Set HuggingFace token for gated models
export HUGGING_FACE_HUB_TOKEN=your-token-here

# Or use huggingface-cli
huggingface-cli login
```

### Port Already in Use

**Problem**: `Address already in use: 0.0.0.0:8000`

**Solution**: Change port or kill existing process
```bash
# Use different port
--port 8001

# Or find and kill process on port 8000
lsof -ti:8000 | xargs kill -9  # Linux/macOS
```

## Next Steps

- [vLLM Optimization Guide](vllm-optimization-guide.md) - Performance tuning and best practices
- [Official vLLM Documentation](https://docs.vllm.ai/)
- [Grid Worker README](../README.md)

## Resources

- **vLLM GitHub**: https://github.com/vllm-project/vllm
- **vLLM Documentation**: https://docs.vllm.ai/
- **HuggingFace Models**: https://huggingface.co/models
- **AI Power Grid**: https://aipowergrid.io
