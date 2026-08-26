import stac_pydantic.api as stac_api


class Search(stac_api.Search):
    """STAC Item Search request body, extended with page-based pagination.

    `stac_pydantic.api.Search` only models `limit`; this app's list endpoints
    are all paginated with `page`/`page_size`, so `POST /search` needs the
    same `page` field to stay consistent with `GET /search` and every other
    listing endpoint in this app.
    """

    page: int | None = None
