from app.config import BACKEND_DIR, PROJECT_ROOT, get_settings

s = get_settings()
print("root", PROJECT_ROOT, (PROJECT_ROOT / ".env").exists())
print("backend", BACKEND_DIR, (BACKEND_DIR / ".env").exists())
print(
    "values",
    s.app_env,
    s.llm_provider,
    s.embedding_provider,
    s.ollama_model,
    s.ollama_num_ctx,
    s.ollama_timeout_sec,
)
