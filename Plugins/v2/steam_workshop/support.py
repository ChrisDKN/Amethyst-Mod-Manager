_ALIASES = {"499450": "292030"}
INSTALL_EXCLUSIONS = {
    "1465460": "Workshop maps need routing outside BepInEx/plugins.",
    "703080": "Workshop blueprints need routing outside win64/ovldata.",
}


def workshop_app_id(game):
    app_id = str(getattr(game, "steam_id", "") or "").strip()
    if not app_id.isascii() or not app_id.isdecimal() or not 0 < int(app_id) < 2**32:
        return ""
    return _ALIASES.get(str(int(app_id)), str(int(app_id)))


def supports_workshop_install(game):
    app_id = workshop_app_id(game)
    return bool(app_id) and app_id not in INSTALL_EXCLUSIONS


def fetch_support(app_id, client, cancel):
    import json
    import urllib.request
    from Utils.ca_bundle import get_ssl_context
    store_supported = None
    try:
        request = urllib.request.Request(
            f'https://store.steampowered.com/api/appdetails?appids={app_id}&filters=categories',
            headers={'Accept': 'application/json', 'User-Agent': 'Amethyst-Mod-Manager'})
        with urllib.request.urlopen(request, timeout=10, context=get_ssl_context()) as response:
            result = json.load(response)[app_id]
        if result.get('success') is not True:
            raise ValueError('Steam did not return app details')
        categories = result['data']['categories']
        if (not isinstance(categories, list) or any(not isinstance(category, dict)
                or type(category.get('id')) is not int for category in categories)):
            raise ValueError('Steam returned invalid app categories')
        store_supported = any(category['id'] == 30 for category in categories)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if cancel.is_set():
        from .download import DownloadCancelled
        raise DownloadCancelled()
    if store_supported:
        return True
    entries, total, _ = client.listing(app_id, page_size=1, cancel=cancel)
    if any(entry.file_type == 0 for entry in entries):
        return True
    if total != 0 or store_supported is None:
        raise ValueError('Steam did not confirm Workshop availability')
    return False
