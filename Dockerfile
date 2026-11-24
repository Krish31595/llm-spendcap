FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/Krish31595/llm-spendcap" \
      org.opencontainers.image.description="Hard budgets for OpenAI, Anthropic and OpenRouter API keys." \
      org.opencontainers.image.licenses="MIT"
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .
EXPOSE 8787
ENTRYPOINT ["spendcap", "-c", "/config/spendcap.toml"]
CMD ["serve", "--host", "0.0.0.0"]
