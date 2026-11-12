"""Local entrypoint: uvicorn app.main:app"""
from app.main import app
from app.config import DEFAULT_PORT

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=DEFAULT_PORT)
