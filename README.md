# Grid Inference Worker

Turn-key text inference worker for [AI Power Grid](https://aipowergrid.io). Run a local model, connect to the Grid, and start earning.

![Setup Wizard](assets/screenshot.png)

## Download

Grab the latest binary for your platform from [Releases](https://github.com/AIPowerGrid/grid-inference-worker/releases):

| Platform | File |
|----------|------|
| Windows x64 | `grid-inference-worker-windows-x64.exe` |
| macOS ARM64 | `grid-inference-worker-macos-arm64.zip` |
| Linux x64 | `grid-inference-worker-linux-x64` |
| Linux ARM64 | `grid-inference-worker-linux-arm64` |

**Windows** — Double-click the exe. A setup wizard opens in your browser at `http://localhost:7861`.

**macOS** — Unzip, then open `Grid Inference Worker.app`.

**Linux** — `chmod +x grid-inference-worker-linux-x64 && ./grid-inference-worker-linux-x64`

No Python or dependencies needed. Just install a backend (Ollama is easiest), run the worker, and follow the wizard.

## Headless / Server

For servers, containers, or automation, run in headless mode with CLI flags or environment variables.

### CLI flags

```bash
grid-inference-worker --headless \
  --model llama3.2:3b \
  --backend-url http://127.0.0.1:11434 \
  --api-key YOUR_API_KEY \
  --worker-name my-worker
```

Passing any flag automatically enables headless mode (no GUI).

### Environment variables

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

| Variable | Default | Description |
|----------|---------|-------------|
| `GRID_API_KEY` | *(required)* | Your Grid API key |
| `MODEL_NAME` | | Model to serve (e.g. `llama3.2:3b`) |
| `BACKEND_TYPE` | `ollama` | `ollama` or `openai` |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Ollama endpoint |
| `OPENAI_URL` | `http://127.0.0.1:8000/v1` | OpenAI-compatible endpoint (vLLM, SGLang, etc.) |
| `OPENAI_API_KEY` | | API key for OpenAI-compatible backend |
| `GRID_WORKER_NAME` | `Text-Inference-Worker` | Worker name on the grid |
| `GRID_MAX_LENGTH` | `4096` | Max generation length |
| `GRID_MAX_CONTEXT_LENGTH` | `4096` | Max context window (auto-detected in GUI mode) |
| `GRID_NSFW` | `true` | Accept NSFW jobs |
| `WALLET_ADDRESS` | | Base chain wallet for rewards |

Then run:

```bash
grid-inference-worker --headless
```

### All CLI options

```
--headless              Run without GUI (terminal only)
--model NAME            Model name (e.g. llama3.2:3b)
--backend-url URL       Backend URL (e.g. http://127.0.0.1:11434)
--api-key KEY           Grid API key
--worker-name NAME      Worker name on the grid
--no-setup              Fail instead of prompting for missing config
--install-service       Install as a system service (auto-start on boot)
--uninstall-service     Remove the system service
--service-status        Check if the service is installed
```

## Run from Source

Requires Python 3.9+.

```bash
pip install -e .
grid-inference-worker
```

On Windows you can also use:

```powershell
.\scripts\run.ps1
```

## Docker

```bash
cp .env.example .env
# Edit .env with your values
docker compose up -d
```

The dashboard is available at `http://localhost:7861`.

## Install as a Service

Run the worker on boot without needing to stay logged in. Works on Windows (startup registry), Linux (systemd), and macOS (launchd).

```bash
# Configure the worker first (run it once to set up .env), then:
grid-inference-worker --install-service

# Check status
grid-inference-worker --service-status

# Remove
grid-inference-worker --uninstall-service
```

## Supported Backends

| Backend | Type | Setup |
|---------|------|-------|
| [Ollama](https://ollama.com) | `ollama` | Install Ollama, `ollama pull llama3.2:3b`, done |
| [LM Studio](https://lmstudio.ai) | `ollama` | Load a model, enable server in Developer tab |
| [vLLM](https://github.com/vllm-project/vllm) | `openai` | `--served-model-name` + set `OPENAI_URL` |
| [SGLang](https://github.com/sgl-project/sglang) | `openai` | Point `OPENAI_URL` at SGLang's OpenAI endpoint |
| [LMDeploy](https://github.com/InternLM/lmdeploy) | `openai` | `lmdeploy serve api_server` + set `OPENAI_URL` |
| [KoboldCpp](https://github.com/LostRuins/koboldcpp) | `openai` | Enable OpenAI-compatible endpoint |

**Ollama** is the easiest way to get started. The setup wizard auto-detects it and lets you pick a model.

For any backend that exposes an **OpenAI-compatible API** (`/v1/chat/completions`), set `BACKEND_TYPE=openai` and point `OPENAI_URL` at it.
