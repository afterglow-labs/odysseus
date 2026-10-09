"""Authenticated server video browsing shared by H3 and BFS clients."""
import asyncio

from fastapi import Request
from fastapi.responses import JSONResponse

from src.video_server_files import list_server_videos


def add_server_video_file_routes(router, owner_callback):
    @router.get('/server-files')
    async def server_files(request: Request, root: str = 'e', path: str = '',
                           offset: int = 0, limit: int = 200, search: str = ''):
        owner_callback(request)
        listing = await asyncio.to_thread(list_server_videos, root=root, path=path,
                                          offset=offset, limit=limit, search=search)
        return JSONResponse(listing, headers={'Cache-Control': 'private, no-store'})
