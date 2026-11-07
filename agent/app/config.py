from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_")

    ollama_base_url: str = "http://ollama:11434"
    default_model: str = "qwen2.5-coder:7b"
    workspace_root: str = "/workspace"
    # Persisted UI state (repo picker, etc.) — mount on Unraid for survives recreate
    data_dir: str = "/data"
    max_agent_steps: int = 40
    shell_timeout_sec: int = 120
    host: str = "0.0.0.0"
    port: int = 8080

    # GitHub (PAT or fine-grained token with repo + issues scope)
    github_token: str = ""
    github_repo: str = ""  # owner/name
    github_default_branch: str = "main"
    github_branch_prefix: str = "cursor/"
    github_branch_suffix: str = "local"
    github_webhook_secret: str = ""
    github_trigger_label: str = "agent"
    github_comment_trigger: str = "@agent"

    # Public URL for webhook setup docs (optional)
    public_base_url: str = ""


settings = Settings()
