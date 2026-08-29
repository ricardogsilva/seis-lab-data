from typing import Final

STAC_VERSION: Final[str] = "1.0.0"

CORE: Final[str] = "https://api.stacspec.org/v1.0.0/core"
ITEM_SEARCH: Final[str] = "https://api.stacspec.org/v1.0.0/item-search"
COLLECTIONS: Final[str] = "https://api.stacspec.org/v1.0.0/collections"
OGC_FEATURES: Final[str] = "https://api.stacspec.org/v1.0.0/ogcapi-features"
OGC_FEATURES_CORE: Final[str] = (
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/core"
)
OGC_FEATURES_OAS30: Final[str] = (
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/oas30"
)
OGC_FEATURES_GEOJSON: Final[str] = (
    "http://www.opengis.net/spec/ogcapi-features-1/1.0/conf/geojson"
)
CHILDREN: Final[str] = "https://api.stacspec.org/v1.0.0-rc.2/children"

CONFORMS_TO: Final[list[str]] = [
    CORE,
    ITEM_SEARCH,
    COLLECTIONS,
    OGC_FEATURES,
    OGC_FEATURES_CORE,
    OGC_FEATURES_OAS30,
    OGC_FEATURES_GEOJSON,
    CHILDREN,
]
