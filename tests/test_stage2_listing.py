"""段2: 計算層（`MemoryStore`）から見た消した跡・非索引・一覧の無作為と前後。

保管層のテスト（test_stage2_forget_priv.py）と違い、こちらは既定の SQLite 実装越しに
「ぷちが実際に呼ぶ経路」を見る。
"""

from __future__ import annotations

from memory_mcp.store import MemoryStore

# ──────────────────────────────────────────────
# 消した跡
# ──────────────────────────────────────────────


async def test_delete_memory_leaves_a_trace_by_default(memory_store: MemoryStore) -> None:
    mem = await memory_store.save("忘れたい話", category="feeling")

    assert await memory_store.delete_memory(mem.id, reason="もう抱えていたくない") is True
    assert await memory_store.get_by_id(mem.id) is None

    markers = await memory_store.list_forget_markers()
    assert [m.memory_id for m in markers] == [mem.id]
    assert markers[0].reason == "もう抱えていたくない"
    assert markers[0].forgotten_at


async def test_merging_does_not_leave_a_forget_trace(memory_store: MemoryStore) -> None:
    """統合は忘却ではない（中身は新しい 1 件に引き継がれる）ので跡を残さない。"""
    first = await memory_store.save("朝に海を見た")
    second = await memory_store.save("朝に海がきれいだった")

    await memory_store.merge_memories(
        source_ids=[first.id, second.id],
        merged_content="朝の海がきれいだった",
        importance=3,
        emotion="happy",
        category="daily",
    )

    assert await memory_store.list_forget_markers() == []


async def test_deleting_a_missing_memory_leaves_no_trace(memory_store: MemoryStore) -> None:
    assert await memory_store.delete_memory("missing-id", reason="消したつもり") is False
    assert await memory_store.list_forget_markers() == []


# ──────────────────────────────────────────────
# index:false
# ──────────────────────────────────────────────


async def test_index_false_memory_is_out_of_search_and_recall(memory_store: MemoryStore) -> None:
    visible = await memory_store.save("金魚の水換えをした")
    hidden = await memory_store.save("金魚の水換えのことは黙っていたい", indexed=False)

    found = {r.memory.id for r in await memory_store.search("金魚の水換え", n_results=10)}
    assert visible.id in found
    assert hidden.id not in found

    recalled = {r.memory.id for r in await memory_store.recall("金魚の水換え", n_results=5)}
    assert hidden.id not in recalled

    # ID 指定では取れるし、新着一覧にも出る
    fetched = await memory_store.get_by_id(hidden.id)
    assert fetched is not None
    assert fetched.indexed is False
    assert hidden.id in {m.id for m in await memory_store.list_recent(limit=10)}


async def test_private_memory_stays_searchable_for_the_petit(memory_store: MemoryStore) -> None:
    private = await memory_store.save("ひとりで考えていたこと", private=True)

    fetched = await memory_store.get_by_id(private.id)
    assert fetched is not None
    assert fetched.private is True

    found = {r.memory.id for r in await memory_store.search("ひとりで考えていた", n_results=10)}
    assert private.id in found


# ──────────────────────────────────────────────
# 一覧: 無作為の 1 件と前後
# ──────────────────────────────────────────────


async def test_listing_without_extras_matches_list_recent(memory_store: MemoryStore) -> None:
    """既定（random=0 / neighbors=False）では段1 までと同じ並び。"""
    for i in range(3):
        await memory_store.save(f"{i} 番目の記憶")

    listing = await memory_store.list_recent_listing(limit=3)
    assert [e.memory.id for e in listing.entries] == [
        m.id for m in await memory_store.list_recent(limit=3)
    ]
    assert all(e.kind == "recent" for e in listing.entries)
    assert all(e.previous is None and e.next is None for e in listing.entries)
    assert listing.forgotten == ()


async def test_listing_mixes_in_random_memories_from_outside_the_window(
    memory_store: MemoryStore,
) -> None:
    saved = [await memory_store.save(f"{i} 番目の記憶") for i in range(6)]
    newest_two = {saved[-1].id, saved[-2].id}

    listing = await memory_store.list_recent_listing(limit=2, random_count=2)

    recent = [e for e in listing.entries if e.kind == "recent"]
    mixed = [e for e in listing.entries if e.kind == "random"]
    assert {e.memory.id for e in recent} == newest_two
    assert len(mixed) == 2
    # 無作為の 1 件は「一覧に出ていない記憶」から選ぶ
    assert {e.memory.id for e in mixed}.isdisjoint(newest_two)


async def test_random_pool_skips_index_false_memories(memory_store: MemoryStore) -> None:
    await memory_store.save("表に出したくない古い話", indexed=False)
    recent = [await memory_store.save(f"{i} 番目の記憶") for i in range(2)]

    listing = await memory_store.list_recent_listing(limit=2, random_count=3)
    mixed = [e for e in listing.entries if e.kind == "random"]
    # 母集団は新着 2 件を除いた索引つきの記憶だけ＝0 件
    assert mixed == []
    assert {e.memory.id for e in listing.entries} == {m.id for m in recent}


async def test_listing_can_add_the_memory_before_and_after_each_one(
    memory_store: MemoryStore,
) -> None:
    saved = [await memory_store.save(f"{i} 番目の記憶") for i in range(3)]

    listing = await memory_store.list_recent_listing(limit=1, neighbors=True)
    assert len(listing.entries) == 1

    entry = listing.entries[0]
    assert entry.memory.id == saved[-1].id
    assert entry.previous is not None
    assert entry.previous.id == saved[-2].id
    assert entry.next is None  # 一番新しい記憶なので後ろは無い


async def test_listing_shows_forget_traces_from_the_same_stretch(
    memory_store: MemoryStore,
) -> None:
    oldest = await memory_store.save("いちばん古い記憶")
    doomed = await memory_store.save("すぐ消す記憶")
    await memory_store.save("いちばん新しい記憶")

    await memory_store.delete_memory(doomed.id, reason="言いたくなくなった")

    listing = await memory_store.list_recent_listing(limit=5)
    assert oldest.id in {e.memory.id for e in listing.entries}
    assert [m.memory_id for m in listing.forgotten] == [doomed.id]
    assert listing.forgotten[0].reason == "言いたくなくなった"
    # 跡には本文が入らない
    assert "すぐ消す記憶" not in str(listing.forgotten)
