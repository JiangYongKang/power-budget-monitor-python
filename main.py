import uvicorn
from fastapi import FastAPI

from power_budget.api import create_app
from power_budget.seed import build_service

app: FastAPI = create_app(build_service())


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
