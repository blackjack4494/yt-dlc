# coding: utf-8
from __future__ import unicode_literals

import re
import json

from .common import InfoExtractor
from ..utils import ExtractorError


class PinterestIE(InfoExtractor):
    _VALID_URL = r"https?://(?:www\.)?pinterest\.(?:com|fr|de|ch|jp|cl|ca|it|co.uk|nz|ru|com.au|at|pt|co.kr|es|com.mx|dk|ph|biz|th|com.pt|com.uy|co|nl|info|kr|ie|vn|com.vn|ec|mx|in|pe|co.at|hu|co.in|co.nz|id|co.id|com.ec|com.py|engineering|tw|be|uk|com.bo|com.pe)/pin/(?P<id>[0-9]+)"
    _TEST = {
        "url": "https://www.pinterest.ca/pin/45599014963532024",
        # "md5": "f51309dfca161c82a9cccb835ab10572",
        "info_dict": {
            "id": "45599014963532024",
            "ext": "mp4",
            "title": "Look at the lil' chuskys and all of their fluff!!!",
            "thumbnail": "https://i.pinimg.com/136x136/61/4f/8c/614f8c789fe217e2fb26d7911e01cf79.jpg",
            "uploader": "cathwoman82",
            "description": "Look at the lil' chuskys and all of their fluff!!!",
        },
    }

    def _real_extract(self, url):
        video_id = self._match_id(url)
        clean_url = re.search(self._VALID_URL, url).group(0)

        webpage = self._download_webpage(clean_url, video_id)

        pin_info_json = self._search_regex(
            r"<script id=\"initial-state\" type=\"application/json\">(.+?)</script>",
            webpage,
            "Pin data JSON",
        )
        pin_info_full = json.loads(pin_info_json)
        pin_info = next(
            (
                r
                for r in pin_info_full["resourceResponses"]
                if r["name"] == "PinResource"
            ),
            None,
        )

        if pin_info:
            pin_data = pin_info["response"]["data"]
            video_urls = pin_data.get("videos", {}).get("video_list", {})
            video_data = video_urls.get("V_HLSV4")
            video_url = video_data.get("url")
            video_thumbs = [
                v
                for (k, v) in pin_data.get("images").items()
                if v.get("width") == v.get("height")
            ]
            if not video_url:
                raise ExtractorError("Can't find a video stream URL")
            description = pin_data.get("description")
            title = (
                description
                or pin_data.get("title").strip()
                or pin_data.get("grid_title")
                or "pinterest_video"
            )
            pinner = pin_data.get("pinner", {})
            uploader = pinner.get("username")
        else:
            raise ExtractorError("Can't find Pin data")
        return {
            "id": video_id,
            "title": title,
            "description": description,
            "uploader": uploader,
            "url": video_url,
            "ext": "mp4",
            "manifest_url": video_url,
            "thumbnails": video_thumbs,
        }
