from dataclasses import asdict

from .jobs import JobError


def thunderstore_runner(community, options, *, filters=False):
    def run(_log, progress):
        from Thunderstore.thunderstore_api import fetch_filters, fetch_listing, package_url, PAGE_SIZE

        progress(0, 1, "Reading Thunderstore catalogue")
        if filters:
            result = fetch_filters(community)
            if result is None:
                raise JobError("thunderstore_catalog_unavailable", "Could not read Thunderstore filters.")
            return {"community": community, **asdict(result)}
        try:
            rows, total = fetch_listing(community, strict=True, **options)
        except (ValueError, TypeError) as exc:
            raise JobError("thunderstore_catalog_unavailable", "Could not read the Thunderstore catalogue.") from exc
        items = [{**asdict(row), "package_id": row.package_id,
                  "url": package_url(community, row.namespace, row.name)} for row in rows]
        page = options["page"]
        progress(1, 1, "Reading Thunderstore catalogue")
        return {"community": community, "packages": items, "total": total, "page": page,
                "page_size": PAGE_SIZE, "next_page": page + 1 if (page + 1) * PAGE_SIZE < total else None}
    return run


def modio_runner(options):
    def run(_log, progress):
        from .modio_versions import api_client, GAME_ID

        progress(0, 1, "Reading mod.io catalogue")
        with api_client() as api:
            rows, total = api.search_mods(**options)
        progress(1, 1, "Reading mod.io catalogue")
        offset = options["offset"]
        return {"game_id": GAME_ID, "mods": [asdict(row) for row in rows], "total": total,
                "offset": offset, "limit": options["limit"],
                "next_offset": offset + len(rows) if rows and offset + len(rows) < total else None}
    return run
