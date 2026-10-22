"""Unit tests for Bitmagnet GraphQL integration."""

import json
import unittest
from unittest.mock import AsyncMock, patch

from arrstack_mcp import bitmagnet, server


class BitmagnetEnrichItemsTests(unittest.TestCase):
    def test_enrich_formats_size_and_files(self):
        items = [
            {
                "id": "1",
                "title": "Movie 1",
                "seeders": 15,
                "videoCodec": "x265",
                "torrent": {
                    "name": "Movie 1",
                    "size": 1073741824,
                    "files": [
                        {"index": 0, "path": "movie.mkv", "size": 1073741824}
                    ],
                },
            }
        ]

        enriched = bitmagnet._bitmagnet_enrich_items(items)
        self.assertEqual(len(enriched), 1)
        self.assertEqual(enriched[0]["torrent"]["sizeFormatted"], "1.0 GB")
        self.assertEqual(enriched[0]["torrent"]["files"][0]["sizeFormatted"], "1.0 GB")

    def test_enrich_filters_min_seeders(self):
        items = [
            {"id": "1", "seeders": 5},
            {"id": "2", "seeders": 25},
        ]
        enriched = bitmagnet._bitmagnet_enrich_items(items, min_seeders=10)
        self.assertEqual(len(enriched), 1)
        self.assertEqual(enriched[0]["id"], "2")

    def test_enrich_filters_codecs_with_aliases(self):
        items = [
            {"id": "1", "videoCodec": "x265"},
            {"id": "2", "videoCodec": "h264"},
            {"id": "3", "videoCodec": "AV1"},
        ]
        enriched = bitmagnet._bitmagnet_enrich_items(items, video_codecs=["h265"])
        self.assertEqual(len(enriched), 1)
        self.assertEqual(enriched[0]["id"], "1")


class BitmagnetGraphQLSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_simple_search_unconfigured(self):
        with patch.object(server, "BITMAGNET_GRAPHQL_URL", ""):
            out = await bitmagnet.bitmagnet_simple_search("Matrix")
            self.assertIn("Bitmagnet is not configured", out)

    @patch("arrstack_mcp.bitmagnet._bitmagnet_execute_graphql", new_callable=AsyncMock)
    async def test_simple_search_success(self, mock_graphql):
        mock_graphql.return_value = {
            "torrentContent": {
                "search": {
                    "items": [
                        {
                            "id": "t1",
                            "title": "The Matrix 1999",
                            "seeders": 100,
                            "torrent": {"size": 2147483648},
                        }
                    ]
                }
            }
        }

        with patch.object(server, "BITMAGNET_GRAPHQL_URL", "http://bitmagnet:3333/graphql"):
            out = await bitmagnet.bitmagnet_simple_search("Matrix", contentType="movie", limit=5)
            data = json.loads(out)
            self.assertEqual(data["count"], 1)
            self.assertEqual(data["items"][0]["title"], "The Matrix 1999")
            self.assertEqual(data["items"][0]["torrent"]["sizeFormatted"], "2.0 GB")

            mock_graphql.assert_called_once()
            _, kwargs = mock_graphql.call_args
            query_vars = kwargs["variables"] if "variables" in kwargs else mock_graphql.call_args[0][1]
            self.assertEqual(query_vars["input"]["queryString"], "Matrix")
            self.assertEqual(query_vars["input"]["limit"], 5)
            self.assertEqual(query_vars["input"]["facets"]["contentType"]["filter"], ["movie"])

    async def test_complex_search_unconfigured(self):
        with patch.object(server, "BITMAGNET_GRAPHQL_URL", ""):
            out = await bitmagnet.bitmagnet_complex_search(queryString="Inception")
            self.assertIn("Bitmagnet is not configured", out)

    @patch("arrstack_mcp.bitmagnet._bitmagnet_execute_graphql", new_callable=AsyncMock)
    async def test_complex_search_success(self, mock_graphql):
        mock_graphql.return_value = {
            "torrentContent": {
                "search": {
                    "totalCount": 1,
                    "hasNextPage": False,
                    "items": [
                        {
                            "id": "t2",
                            "title": "Inception 2010 1080p",
                            "seeders": 50,
                            "videoCodec": "x264",
                            "torrent": {"size": 4294967296},
                        }
                    ],
                    "aggregations": {"videoResolution": [{"value": "V1080p", "count": 1}]},
                }
            }
        }

        with patch.object(server, "BITMAGNET_GRAPHQL_URL", "http://bitmagnet:3333/graphql"):
            out = await bitmagnet.bitmagnet_complex_search(
                queryString="Inception",
                videoResolutions=["V1080p"],
                minSeeders=10,
            )
            data = json.loads(out)
            self.assertEqual(data["totalCount"], 1)
            self.assertEqual(data["returnedCount"], 1)
            self.assertEqual(data["items"][0]["torrent"]["sizeFormatted"], "4.0 GB")


if __name__ == "__main__":
    unittest.main()
