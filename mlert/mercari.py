"""
Thin async wrapper around the unofficial `mercapi` library.

Everything that touches the network lives here so the matching/learning code
stays pure and testable offline.
"""

import asyncio
import json
import re
import sys
import time
from pathlib import Path

# Every mercari.jp item id looks like this in URLs and in calls to m.item(id).
_ID_PATTERN = re.compile(r"^m\d{6,}$")

_SEARCH_ENUMS = None


def _enums():
    """Locate mercapi's SearchRequestData enum container (path has moved between versions)."""
    global _SEARCH_ENUMS
    if _SEARCH_ENUMS is not None:
        return _SEARCH_ENUMS
    last_err = None
    for modpath in ("mercapi.requests.search", "mercapi.requests", "mercapi"):
        try:
            mod = __import__(modpath, fromlist=["SearchRequestData"])
            _SEARCH_ENUMS = getattr(mod, "SearchRequestData")
            return _SEARCH_ENUMS
        except (ImportError, AttributeError) as e:  # pragma: no cover
            last_err = e
    raise ImportError(
        "Could not find SearchRequestData in the installed mercapi package "
        f"(last error: {last_err}). Check https://github.com/take-kun/mercapi "
        "for the current API and update mlert/mercari.py."
    ) from last_err


class MercariClient:
    """Rate-limited, retrying facade over mercapi."""

    def __init__(self, delay=2.0, max_retries=3, cache_path=None, verbose=True):
        from mercapi import Mercapi

        self._m = Mercapi()
        self.delay = delay
        self.max_retries = max_retries
        self.verbose = verbose
        self._last_call = 0.0
        self._count_cache = {}
        self._cache_path = Path(cache_path) if cache_path else None
        if self._cache_path and self._cache_path.exists():
            try:
                self._count_cache = json.loads(self._cache_path.read_text("utf-8"))
            except Exception:
                self._count_cache = {}

    # -- plumbing ---------------------------------------------------------

    async def _throttle(self):
        wait = self.delay - (time.monotonic() - self._last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.monotonic()

    async def _retrying(self, coro_factory, what):
        delay = 2.0
        for attempt in range(1, self.max_retries + 1):
            await self._throttle()
            try:
                return await coro_factory()
            except Exception as e:
                if attempt == self.max_retries:
                    raise
                if self.verbose:
                    print(f"    retry {attempt}/{self.max_retries - 1} for {what}: {e}",
                          file=sys.stderr)
                await asyncio.sleep(delay)
                delay *= 2

    # -- API --------------------------------------------------------------

    async def search(self, query, price_min=None, price_max=None, categories=None,
                     exclude=None, sold=False, sort_newest=True, limit=40):
        E = _enums()
        kwargs = {
            "sort_by": (E.SortBy.SORT_CREATED_TIME if sort_newest else E.SortBy.SORT_SCORE),
            "sort_order": E.SortOrder.ORDER_DESC,
            "status": [E.Status.STATUS_SOLD_OUT if sold else E.Status.STATUS_ON_SALE],
        }
        if price_min is not None:
            kwargs["price_min"] = int(price_min)
        if price_max is not None:
            kwargs["price_max"] = int(price_max)
        if categories:
            kwargs["categories"] = list(categories)
        if exclude:
            kwargs["exclude"] = exclude

        res = await self._retrying(lambda: self._m.search(query, **kwargs), f"search({query!r})")
        items = list(res.items)[:limit]
        num_found = getattr(getattr(res, "meta", None), "num_found", None)

        if items and not getattr(self, "_dumped_sample", False):
            self._dumped_sample = True
            dump_item_attrs(items[0], f"first result of search({query!r})")

        return items, num_found

    async def item(self, item_id):
        return await self._retrying(lambda: self._m.item(item_id), f"item({item_id})")

    async def count(self, term):
        """
        How many live listings match this term? Used as a corpus-wide IDF
        signal: a term matching 300,000 listings identifies a product family,
        one matching 40 identifies a specific item.
        """
        if term in self._count_cache:
            return self._count_cache[term]
        try:
            _items, num_found = await self.search(term, limit=1)
        except Exception as e:
            if self.verbose:
                print(f"    count({term!r}) failed: {e}", file=sys.stderr)
            return None
        if num_found is None:
            return None
        self._count_cache[term] = int(num_found)
        self._flush_cache()
        return int(num_found)

    def _flush_cache(self):
        if not self._cache_path:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(self._count_cache, ensure_ascii=False, indent=1), "utf-8"
            )
        except Exception:
            pass


def item_url(item_id):
    return f"https://jp.mercari.com/item/{item_id}"


def parse_item_id(s):
    """Accept a bare id, a full URL, or a URL with tracking query params."""
    s = (s or "").strip()
    if not s:
        return None
    if "mercari.com" in s or s.startswith("http"):
        tail = s.split("?")[0].rstrip("/").split("/")[-1]
        return tail or None
    return s


def dump_item_attrs(item, context=""):
    """
    Print every public-ish attribute of a mercapi item object to stderr, once.
    Purely diagnostic - lets us see the library's actual field names in the
    GitHub Actions log instead of guessing again offline.
    """
    print(f"DEBUG item sample ({context}): type={type(item).__module__}.{type(item).__name__}",
          file=sys.stderr)
    seen = set()
    for name in dir(item):
        if name.startswith("__"):
            continue
        try:
            v = getattr(item, name)
        except Exception as e:
            print(f"DEBUG   .{name} = <error: {e}>", file=sys.stderr)
            continue
        if callable(v):
            continue
        seen.add(name)
        rep = repr(v)
        if len(rep) > 200:
            rep = rep[:200] + "...(truncated)"
        print(f"DEBUG   .{name} = {rep}", file=sys.stderr)
    if not seen:
        print("DEBUG   (no non-callable public attributes found)", file=sys.stderr)


def _scan_for_id(obj, _depth=0, _seen_ids=None):
    """
    Fallback ID lookup: walk an object's attributes (including single-
    underscore-prefixed ones, since a library may keep the id "private")
    looking for a string shaped like a Mercari item id. Used when none of
    the guessed attribute names hit.
    """
    if _depth > 2 or obj is None:
        return None
    if _seen_ids is None:
        _seen_ids = set()
    if id(obj) in _seen_ids:
        return None
    _seen_ids.add(id(obj))

    values = {}
    for name in dir(obj):
        if name.startswith("__"):
            continue
        try:
            v = getattr(obj, name)
        except Exception:
            continue
        if callable(v):
            continue
        values[name] = v

    for v in values.values():
        if isinstance(v, str) and _ID_PATTERN.match(v):
            return v
    # nested objects (e.g. item.item / item.data wrapping the real fields)
    for v in values.values():
        if hasattr(v, "__dict__") or hasattr(type(v), "__slots__"):
            found = _scan_for_id(v, _depth + 1, _seen_ids)
            if found:
                return found
    return None


def summary_fields(item):
    """
    Pull the fields we need out of a mercapi item object, tolerating naming
    differences between library versions.
    """
    def first(*names, default=None):
        for n in names:
            v = getattr(item, n, None)
            if v not in (None, "", [], {}):
                return v
        return default

    thumb = first("thumbnails", "photos", "photo_urls", "images", "photoUrls", "imageUrls")
    if isinstance(thumb, (list, tuple)):
        thumb = thumb[0] if thumb else None

    item_id = first("id", "id_", "item_id", "itemId", "_id", "productId", "product_id")
    if not item_id:
        item_id = _scan_for_id(item)

    created = first("created", "created_at", "createdAt")
    if hasattr(created, "isoformat"):
        created = created.isoformat()

    return {
        "id": item_id,
        "name": first("name", "title", "productName", default=""),
        "price": first("price", "productPrice", default=None),
        "thumbnail": thumb,
        "seller_id": first("seller_id", "sellerId"),
        "status": first("status"),
        "created": created,
        "condition_id": first("item_condition_id", "itemConditionId"),
        "category_id": first("category_id", "categoryId"),
    }
