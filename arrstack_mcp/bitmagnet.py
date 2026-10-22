"""Bitmagnet GraphQL integration for arrstack-mcp.

Provides tools for querying Bitmagnet's native GraphQL endpoint to discover
DHT torrents and inspect indexed metadata.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Literal, Optional

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger("arrstack-mcp.bitmagnet")

ContentTypeLiteral = Literal[
    "movie", "tv_show", "music", "ebook", "comic",
    "audiobook", "game", "software", "xxx", "null"
]

VideoResolutionLiteral = Literal[
    "V360p", "V480p", "V540p", "V576p", "V720p", "V1080p", "V1440p", "V2160p", "V4320p"
]

VideoSourceLiteral = Literal[
    "BluRay", "WEBDL", "WEBRip", "WEB", "TV", "HDTV",
    "DVD", "CAM", "TELESYNC", "TELECINE", "WORKPRINT"
]

VideoCodecLiteral = Literal[
    "x264", "x265", "H264", "H265", "AV1", "XviD", "DivX", "MPEG2", "MPEG4", "VC1", "VP9"
]

OrderFieldLiteral = Literal[
    "relevance", "published_at", "updated_at", "size",
    "files_count", "seeders", "leechers", "name", "info_hash"
]


class OrderByInput(BaseModel):
    field: OrderFieldLiteral = "seeders"
    descending: bool = True


_BITMAGNET_SIMPLE_SEARCH_QUERY = """
query SimpleTorrentSearch($input: TorrentContentSearchQueryInput!) {
  torrentContent {
    search(input: $input) {
      items {
        id
        infoHash
        title
        contentType
        seeders
        leechers
        publishedAt
        torrent {
          name
          size
          magnetUri
          filesCount
        }
      }
    }
  }
}
"""

_BITMAGNET_COMPLEX_SEARCH_QUERY = """
query ComplexTorrentSearch($input: TorrentContentSearchQueryInput!) {
  torrentContent {
    search(input: $input) {
      totalCount
      hasNextPage
      items {
        id
        infoHash
        title
        releaseGroup
        contentType
        languages {
          id
          name
        }
        videoResolution
        videoSource
        videoCodec
        video3d
        videoModifier
        seeders
        leechers
        publishedAt
        torrent {
          name
          size
          magnetUri
          filesCount
          tagNames
          files {
            index
            path
            size
          }
        }
        content {
          title
          releaseYear
          overview
          collections {
            name
            type
          }
        }
      }
      aggregations {
        contentType { value count }
        videoResolution { value count }
        videoSource { value count }
        releaseYear { value count }
        language { value label count }
      }
    }
  }
}
"""


def _format_size(size_bytes: int | float) -> str:
    from .server import _format_size as server_format_size
    return server_format_size(size_bytes)


def _bitmagnet_enrich_items(
    items: List[Dict[str, Any]],
    min_seeders: Optional[int] = None,
    video_codecs: Optional[List[str]] = None,
    max_files_per_torrent: int = 25,
) -> List[Dict[str, Any]]:
    """Filter by min_seeders and video_codecs, format size, and limit file lists."""
    processed = []
    allowed_codecs = {c.lower() for c in video_codecs} if video_codecs else None

    if allowed_codecs:
        if "x265" in allowed_codecs:
            allowed_codecs.add("h265")
        if "h265" in allowed_codecs:
            allowed_codecs.add("x265")
        if "x264" in allowed_codecs:
            allowed_codecs.add("h264")
        if "h264" in allowed_codecs:
            allowed_codecs.add("x264")

    for item in items:
        seeders = item.get("seeders") or 0
        if min_seeders is not None and seeders < min_seeders:
            continue

        if allowed_codecs:
            codec = (item.get("videoCodec") or "").strip().lower()
            if not codec or codec not in allowed_codecs:
                continue

        torrent = item.get("torrent")
        if isinstance(torrent, dict):
            if torrent.get("size") is not None:
                torrent["sizeFormatted"] = _format_size(torrent["size"])

            files = torrent.get("files")
            if isinstance(files, list) and len(files) > 0:
                total_files = len(files)
                for f in files[:max_files_per_torrent]:
                    if isinstance(f, dict) and f.get("size") is not None:
                        f["sizeFormatted"] = _format_size(f["size"])
                if total_files > max_files_per_torrent:
                    torrent["files"] = files[:max_files_per_torrent]
                    torrent["filesTruncated"] = True
                    torrent["filesTotalCount"] = total_files

        processed.append(item)
    return processed


async def _bitmagnet_execute_graphql(query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a GraphQL POST request to Bitmagnet with error handling."""
    from . import server
    url = getattr(server, "BITMAGNET_GRAPHQL_URL", "")
    timeout = getattr(server, "BITMAGNET_TIMEOUT", 30.0)

    if not url:
        raise ValueError("Bitmagnet is not configured. Set BITMAGNET_GRAPHQL_URL or BITMAGNET_URL.")

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
        response = await client.post(
            url,
            json={"query": query, "variables": variables},
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()

        if "errors" in payload and payload["errors"]:
            error_messages = "; ".join(err.get("message", str(err)) for err in payload["errors"])
            raise RuntimeError(f"GraphQL error from Bitmagnet: {error_messages}")

        return payload.get("data", {})


async def bitmagnet_simple_search(
    query: str,
    contentType: Optional[ContentTypeLiteral] = None,
    limit: int = 10,
) -> str:
    """Execute a fast full-text search on Bitmagnet for indexed torrents.

    Args:
        query: Search term (e.g. title, actor, infoHash).
        contentType: Optional category filter (e.g. movie, tv_show, music, book).
        limit: Result limit (1-50, default 10).
    """
    from . import server
    if not getattr(server, "BITMAGNET_GRAPHQL_URL", ""):
        return "Bitmagnet is not configured. Set BITMAGNET_GRAPHQL_URL or BITMAGNET_URL."

    limit = max(1, min(50, limit))
    input_vars: Dict[str, Any] = {
        "queryString": query,
        "limit": limit,
        "orderBy": [{"field": "seeders", "descending": True}]
    }

    if contentType:
        input_vars["facets"] = {"contentType": {"filter": [contentType]}}

    try:
        data = await _bitmagnet_execute_graphql(_BITMAGNET_SIMPLE_SEARCH_QUERY, {"input": input_vars})
        search_res = data.get("torrentContent", {}).get("search", {})
        items = _bitmagnet_enrich_items(search_res.get("items", []))
        return json.dumps({"count": len(items), "items": items}, indent=2, ensure_ascii=False)
    except Exception as error:
        logger.error("bitmagnet_simple_search failed: %s", error)
        return json.dumps({"error": str(error)}, indent=2, ensure_ascii=False)


async def bitmagnet_complex_search(
    queryString: Optional[str] = None,
    contentTypes: Optional[List[ContentTypeLiteral]] = None,
    videoResolutions: Optional[List[VideoResolutionLiteral]] = None,
    videoSources: Optional[List[VideoSourceLiteral]] = None,
    videoCodecs: Optional[List[VideoCodecLiteral]] = None,
    languages: Optional[List[str]] = None,
    releaseYear: Optional[int] = None,
    minSeeders: Optional[int] = None,
    tags: Optional[List[str]] = None,
    orderBy: Optional[OrderByInput] = None,
    orderByField: OrderFieldLiteral = "seeders",
    orderByDescending: bool = True,
    limit: int = 20,
    page: int = 1,
    includeTotalCount: bool = True,
    maxFilesPerTorrent: int = 25,
) -> str:
    """Execute an advanced multi-faceted search on Bitmagnet with filtering and aggregations.

    Args:
        queryString: Full-text search query string.
        contentTypes: Content types to include (e.g. ['movie', 'music']).
        videoResolutions: Video resolutions (e.g. ['V1080p', 'V2160p']).
        videoSources: Video sources (e.g. ['BluRay', 'WEBDL', 'WEB', 'HDTV']).
        videoCodecs: Video codecs (e.g. ['x265', 'AV1', 'H264']).
        languages: ISO 639-1 language codes (e.g. ['it', 'en']).
        releaseYear: Content release year.
        minSeeders: Minimum number of active seeders required.
        tags: Torrent tags filter.
        orderBy: Optional OrderByInput object.
        orderByField: Field to sort by if orderBy omitted (default: seeders).
        orderByDescending: Sort descending if orderBy omitted (default: True).
        limit: Number of items per page (default: 20, max: 100).
        page: Page number (1-based).
        includeTotalCount: Whether to include total count of matching results.
        maxFilesPerTorrent: Maximum detailed files to list per torrent (default: 25).
    """
    from . import server
    if not getattr(server, "BITMAGNET_GRAPHQL_URL", ""):
        return "Bitmagnet is not configured. Set BITMAGNET_GRAPHQL_URL or BITMAGNET_URL."

    limit = max(1, min(100, limit))
    page = max(1, page)

    facets: Dict[str, Any] = {
        "contentType": {"aggregate": True},
        "videoResolution": {"aggregate": True},
        "videoSource": {"aggregate": True},
        "releaseYear": {"aggregate": True},
        "language": {"aggregate": True},
    }

    if contentTypes:
        facets["contentType"]["filter"] = contentTypes
    if videoResolutions:
        facets["videoResolution"]["filter"] = videoResolutions
    if videoSources:
        mapped_sources = set()
        for src in videoSources:
            if src == "WEB":
                mapped_sources.update(["WEBDL", "WEBRip"])
            elif src == "HDTV":
                mapped_sources.add("TV")
            else:
                mapped_sources.add(src)
        facets["videoSource"]["filter"] = list(mapped_sources)
    if languages:
        facets["language"]["filter"] = languages
    if releaseYear is not None:
        facets["releaseYear"]["filter"] = [releaseYear]
    if tags:
        facets["torrentTag"] = {"filter": tags}

    if orderBy:
        order_clause = [{"field": orderBy.field, "descending": orderBy.descending}]
    else:
        order_clause = [{"field": orderByField, "descending": orderByDescending}]

    input_vars: Dict[str, Any] = {
        "limit": limit,
        "page": page,
        "totalCount": includeTotalCount,
        "hasNextPage": True,
        "orderBy": order_clause,
        "facets": facets,
    }

    if queryString:
        input_vars["queryString"] = queryString

    try:
        data = await _bitmagnet_execute_graphql(_BITMAGNET_COMPLEX_SEARCH_QUERY, {"input": input_vars})
        search_res = data.get("torrentContent", {}).get("search", {})

        items = _bitmagnet_enrich_items(
            search_res.get("items", []),
            min_seeders=minSeeders,
            video_codecs=videoCodecs,
            max_files_per_torrent=maxFilesPerTorrent,
        )

        result = {
            "totalCount": search_res.get("totalCount"),
            "hasNextPage": search_res.get("hasNextPage"),
            "returnedCount": len(items),
            "items": items,
            "aggregations": search_res.get("aggregations", {})
        }

        return json.dumps(result, indent=2, ensure_ascii=False)
    except Exception as error:
        logger.error("bitmagnet_complex_search failed: %s", error)
        return json.dumps({"error": str(error)}, indent=2, ensure_ascii=False)


def register_tools(mcp) -> None:
    """Register bitmagnet tools on FastMCP instance."""
    mcp.tool()(bitmagnet_simple_search)
    mcp.tool()(bitmagnet_complex_search)
