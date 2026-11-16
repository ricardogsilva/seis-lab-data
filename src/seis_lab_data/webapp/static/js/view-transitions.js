/*
 * Cross-document view transitions following the catalog hierarchy.
 *
 * A navigation from a page to one of its descendants is a drill-down, and from a page
 * to one of its ancestors a drill-up. The ancestors of a page are the links of its
 * breadcrumbs, its direct descendants the links of its list items, and a page also
 * counts as the ancestor of the pages drilled down into from it - such as the list
 * of survey missions for a survey mission, which is not in its breadcrumbs.
 *
 * Both directions are marked as types of the view transition, which the stylesheet
 * animates as a zoom in or a zoom out of the whole page. When the outgoing page knows
 * the relationship, the elements describing the same resource on both pages also
 * morph into each other, as both pages name them with the same
 * `view-transition-name`: on a drill-down, the list item of the destination becomes
 * the main card of its detail page, and on a drill-up the main card shrinks back into
 * its list item, when the destination lists it.
 *
 * The outgoing page tells the incoming one which elements it named through the session
 * storage, as the incoming page can only name its own elements. Names are assigned only
 * for the duration of a navigation, since a name must be unique within a page and the
 * list pages hold many items. Any other navigation keeps the plain cross-fade.
 *
 * Browsers without support for cross-document view transitions never fire the
 * `pageswap` and `pagereveal` events with a view transition and navigate as usual.
 */

const DRILL_DOWN = "drill-down";
const DRILL_UP = "drill-up";
const ITEM_NAMES = {card: "sld-item-card", title: "sld-item-title", visual: "sld-item-visual"};
const BREADCRUMB_LINKS_SELECTOR = 'nav[aria-label="breadcrumbs"] a';
const MESSAGE_STORAGE_KEY = "sld:view-transition";
const ENTERED_FROM_STORAGE_KEY_PREFIX = "sld:view-transition-entered-from:";

function findLinkByPathname(selector, pathname) {
    for (const link of document.querySelectorAll(selector)) {
        if (new URL(link.href, location.href).pathname === pathname) {
            return link;
        }
    }
    return null;
}

function findListItemElements(pathname) {
    const link = findLinkByPathname(".list-item-card a.list-item-link", pathname);
    if (!link) {
        return null;
    }
    const card = link.closest(".list-item-card");
    return {
        card,
        title: link.closest(".card-title"),
        visual: card.querySelector(".list-item-thumb"),
    };
}

function findDetailElements() {
    const card = document.querySelector("[data-view-transition-detail]");
    return {
        card,
        title: document.querySelector("h1.page-title"),
        visual: card?.querySelector("bbox-map-readonly"),
    };
}

function assignNames(itemElements) {
    for (const [key, name] of Object.entries(ITEM_NAMES)) {
        if (itemElements?.[key]) {
            itemElements[key].style.viewTransitionName = name;
        }
    }
}

function clearNames() {
    for (const name of Object.values(ITEM_NAMES)) {
        for (const element of document.querySelectorAll(`[style*="${name}"]`)) {
            element.style.viewTransitionName = "";
        }
    }
}

function readStorage(key) {
    try {
        return JSON.parse(sessionStorage.getItem(key) || "null");
    } catch {
        return null;
    }
}

function writeStorage(key, value) {
    try {
        if (value === null) {
            sessionStorage.removeItem(key);
        } else {
            sessionStorage.setItem(key, JSON.stringify(value));
        }
    } catch {
        // without session storage, only the zoom of the whole page is played
    }
}

window.addEventListener("pageswap", (event) => {
    if (!event.viewTransition || !event.activation?.entry?.url) {
        return;
    }
    const destination = new URL(event.activation.entry.url).pathname;
    const listItemElements = findListItemElements(destination);
    const breadcrumbLink = findLinkByPathname(BREADCRUMB_LINKS_SELECTOR, destination);
    const enteredFrom = readStorage(ENTERED_FROM_STORAGE_KEY_PREFIX + location.pathname);
    let direction = null;
    if (listItemElements) {
        direction = DRILL_DOWN;
        assignNames(listItemElements);
    } else if (breadcrumbLink || destination === enteredFrom) {
        direction = DRILL_UP;
        assignNames(findDetailElements());
    }
    writeStorage(
        MESSAGE_STORAGE_KEY,
        direction && {from: location.pathname, to: destination, direction},
    );
});

window.addEventListener("pagereveal", (event) => {
    if (!event.viewTransition || !navigation.activation?.from?.url) {
        return;
    }
    const origin = new URL(navigation.activation.from.url).pathname;
    const message = readStorage(MESSAGE_STORAGE_KEY);
    writeStorage(MESSAGE_STORAGE_KEY, null);
    const outgoingPageNamedElements = (
        message?.from === origin && message?.to === location.pathname
    );
    let direction = outgoingPageNamedElements ? message.direction : null;
    if (!direction && findLinkByPathname(BREADCRUMB_LINKS_SELECTOR, origin)) {
        direction = DRILL_DOWN;
    } else if (!direction && findListItemElements(origin)) {
        direction = DRILL_UP;
    }
    if (!direction) {
        return;
    }
    if (direction === DRILL_DOWN) {
        writeStorage(ENTERED_FROM_STORAGE_KEY_PREFIX + location.pathname, origin);
    }
    event.viewTransition.types?.add(direction);
    if (outgoingPageNamedElements && direction === DRILL_DOWN) {
        assignNames(findDetailElements());
    } else if (outgoingPageNamedElements) {
        assignNames(findListItemElements(origin));
    }
    event.viewTransition.finished.finally(clearNames);
});

// a page restored from the back/forward cache keeps the names given as it was left
window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
        clearNames();
    }
});
