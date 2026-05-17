import httpx
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class ImmichClient:
    def __init__(self, url: str, api_key: str, verify_ssl: bool = True):
        self.url = url.rstrip('/')
        self.headers = {
            "x-api-key": api_key,
            "Accept": "application/json"
        }
        self.client = httpx.AsyncClient(
            base_url=f"{self.url}/api", 
            headers=self.headers, 
            timeout=30.0,
            verify=verify_ssl
        )

    async def get_all_albums(self):
        try:
            response = await self.client.get("/albums")
            response.raise_for_status()
            return response.json()
        except httpx.ConnectError as e:
            logger.error(f"Connection error to Immich: {e}")
            raise RuntimeError(f"Could not connect to Immich at {self.url}. Check DNS/Network.") from e
        except Exception as e:
            logger.error(f"Unexpected error fetching albums: {e}")
            raise

    async def find_album_by_name(self, name: str):
        albums = await self.get_all_albums()
        for album in albums:
            if album['albumName'].lower() == name.lower():
                return album
        return None

    async def get_album_assets(self, album_id: str):
        response = await self.client.get(f"/albums/{album_id}")
        response.raise_for_status()
        album_data = response.json()
        return album_data.get('assets', [])

    async def download_asset(self, asset_id: str, is_video: bool = False):
        # For videos, we prefer the transcoded version for better Telegram compatibility
        if is_video:
            try:
                # '/video/playback' endpoint returns the encoded/web-compatible version if exists,
                # or falls back to original if not.
                response = await self.client.get(f"/assets/{asset_id}/video/playback")
                if response.status_code == 200:
                    return response.content
                logger.warning(f"Unexpected status {response.status_code} for playback {asset_id}")
            except Exception as e:
                logger.warning(f"Could not fetch playback video for {asset_id}: {e}")

        # Fallback to original for photos or if video playback failed
        response = await self.client.get(f"/assets/{asset_id}/original")
        response.raise_for_status()
        return response.content

    async def close(self):
        await self.client.aclose()
