import os

from fastapi import FastAPI

app = FastAPI()


@app.get("/")
def read_root():
    return {"instance": os.environ.get("INSTANCE_ID", None)}
