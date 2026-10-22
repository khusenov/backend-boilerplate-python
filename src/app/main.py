from fastapi import FastAPI


def create_app() -> FastAPI:
    return FastAPI(title="backend-boilerplate")
