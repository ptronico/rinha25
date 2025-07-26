import logging

import httpx


class HttpClient:
    def __init__(self, base_url: str, name: str):
        self.client = None
        self.base_url = base_url
        self.name = name

    async def connect(self):
        if self.client is None or self.client.is_closed:
            self.client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(
                    connect=5.0,
                    read=5.0,
                    write=5.0,
                    pool=5.0,
                ),
                # TODO: entender melhor como esses parâmetros afetam o sistema
                limits=httpx.Limits(
                    max_connections=50,
                    max_keepalive_connections=20,
                    keepalive_expiry=5.0,
                ),
            )
            logging.warning(f"[HttpClient] {self.name} connected")

    async def disconnect(self):
        if self.client and not self.client.is_closed:
            await self.client.aclose()
            logging.warning(f"[HttpClient] {self.name} disconnected")

    async def reconnect(self):
        await self.disconnect()
        await self.connect()

    async def get(self, endpoint: str, retry=False):
        await self.connect()
        try:
            response = await self.client.get(f"{self.base_url}{endpoint}")
            response.raise_for_status()
            return response.json()
        except httpx.RequestError:
            if not retry:
                await self.reconnect()
                return await self.get(endpoint, retry=True)
            raise

    async def post(self, endpoint: str, payload: dict, retry=False):
        await self.connect()
        try:
            response = await self.client.post(f"{self.base_url}{endpoint}", json=payload)
            response.raise_for_status()
            return response.json()
        except httpx.RequestError:
            if not retry:
                await self.reconnect()
                return await self.posts(endpoint, payload, retry=True)
            raise
