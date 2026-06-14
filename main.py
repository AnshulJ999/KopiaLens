"""Entrypoint: run KopiaLens (uvicorn) on 127.0.0.1:7123."""
import uvicorn
from config import HOST, PORT

if __name__ == "__main__":
    uvicorn.run("app.main:app", host=HOST, port=PORT, reload=False)
