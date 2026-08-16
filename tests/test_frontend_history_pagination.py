from pathlib import Path
from unittest import TestCase, main


ROOT = Path(__file__).resolve().parents[1]
API = ROOT / "frontend" / "src" / "api" / "generation.js"
STORE = ROOT / "frontend" / "src" / "composables" / "useGenerationHistory.js"
OBSERVER = ROOT / "frontend" / "src" / "composables" / "useInfiniteScrollSentinel.js"
PLAYGROUND = ROOT / "frontend" / "src" / "components" / "Playground" / "index.vue"
GALLERY = ROOT / "frontend" / "src" / "components" / "Gallery" / "index.vue"
REGION_EDITOR = ROOT / "frontend" / "src" / "components" / "RegionEditor" / "index.vue"
APP = ROOT / "frontend" / "src" / "App.vue"


class FrontendHistoryPaginationSourceTests(TestCase):
    def test_api_accepts_query_params_without_breaking_no_arg_callers(self):
        source = API.read_text(encoding="utf-8")

        self.assertIn("listSessions(params = {})", source)
        self.assertIn("apiClient.get('/sessions', { params })", source)

    def test_api_exposes_session_detail_endpoint_for_full_parameter_reuse(self):
        source = API.read_text(encoding="utf-8")

        self.assertIn("export function getSession(historyId)", source)
        self.assertIn("apiClient.get(sessionResourcePath(historyId))", source)
        self.assertIn("return encodeURIComponent(String(historyId))", source)

    def test_singleton_store_exposes_cursor_loading_state_without_memory_cap(self):
        source = STORE.read_text(encoding="utf-8")

        self.assertNotIn("MAX_ENTRIES", source)
        self.assertIn("const SESSION_PAGE_SIZE = 30", source)
        self.assertIn("requestGeneration", source)
        self.assertIn("activeLoadMoreToken", source)
        self.assertIn("next._origin = 'local'", source)
        self.assertIn("refreshSessions", source)
        self.assertIn("invalidateSessionRequests", source)
        self.assertIn("loadMoreSessions", source)
        self.assertIn("initialLoading", source)
        self.assertIn("loadingMore", source)
        self.assertIn("nextCursor", source)
        self.assertIn("hasMore", source)
        self.assertIn("loadError", source)
        self.assertIn("slice(0, STORAGE_CACHE_LIMIT)", source)
        self.assertIn(
            "sanitizeForStorage(history.value.slice(0, STORAGE_CACHE_LIMIT))",
            source,
        )

    def test_load_more_does_not_auto_retry_while_error_is_visible(self):
        source = STORE.read_text(encoding="utf-8")

        self.assertIn(
            "activeLoadMoreToken ||\n      initialLoading.value ||\n      loadError.value ||",
            source,
        )
        self.assertIn("loadError.value = null\n      return loadMoreSessions()", source)

    def test_next_page_failure_keeps_already_filtered_server_rows_visible(self):
        store = STORE.read_text(encoding="utf-8")
        playground = PLAYGROUND.read_text(encoding="utf-8")

        self.assertIn("serverResultsCurrent", store)
        self.assertIn("serverResultsCurrent.value", playground)

    def test_delete_keeps_inflight_pages_and_tombstones_the_removed_entry(self):
        source = STORE.read_text(encoding="utf-8")
        start = source.index("  function removeEntry(id) {")
        end = source.index("\n  function clearHistory()", start)
        remove_entry = source[start:end]

        self.assertIn("tombstoneIds.add(id)", remove_entry)
        self.assertIn("detailRequests.delete(id)", remove_entry)
        self.assertNotIn("requestGeneration += 1", remove_entry)
        self.assertNotIn("activeRefreshToken = null", remove_entry)
        self.assertNotIn("activeLoadMoreToken = null", remove_entry)
        self.assertIn("tombstoneIds.has(item.id)", source)
        self.assertIn("tombstoneIds = new Set()", source)

    def test_selected_history_entry_falls_back_after_external_removal(self):
        source = PLAYGROUND.read_text(encoding="utf-8")

        self.assertIn("displayHistoryId.value && !items.some", source)
        self.assertIn("displayHistoryId.value = items[0]?.id || null", source)

    def test_playground_loads_full_details_before_reusing_a_server_prompt(self):
        source = PLAYGROUND.read_text(encoding="utf-8")

        self.assertIn("ensureSessionDetails", source)
        self.assertIn("resolveHistoryEntryForReuse", source)
        recall_start = source.index("async function recallHistory(entry)")
        recall_end = source.index("async function deleteHistory(entry)", recall_start)
        recall = source[recall_start:recall_end]
        self.assertLess(
            recall.index("await resolveHistoryEntryForReuse(entry, token)"),
            recall.index("form.prompt ="),
        )
        self.assertIn("SESSION_SUMMARY_PROMPT_MAX", source)

    def test_observer_is_reused_by_both_scroll_roots_and_disconnects(self):
        observer = OBSERVER.read_text(encoding="utf-8")
        playground = PLAYGROUND.read_text(encoding="utf-8")
        gallery = GALLERY.read_text(encoding="utf-8")

        self.assertIn("IntersectionObserver", observer)
        self.assertIn("observer.disconnect()", observer)
        self.assertIn("onBeforeUnmount", observer)
        self.assertIn("useInfiniteScrollSentinel", playground)
        self.assertIn("useInfiniteScrollSentinel", gallery)
        self.assertIn("historyScrollRef", playground)
        self.assertIn("galleryScrollRef", gallery)

    def test_scroll_fallback_is_passive_near_bottom_and_cleans_up(self):
        observer = OBSERVER.read_text(encoding="utf-8")

        self.assertIn("scrollTop", observer)
        self.assertIn("clientHeight", observer)
        self.assertIn("scrollHeight", observer)
        self.assertIn("addEventListener('scroll'", observer)
        self.assertIn("removeEventListener('scroll'", observer)
        self.assertIn("passive: true", observer)
        self.assertIn("loadPending", observer)

    def test_playground_debounces_server_side_query_and_time_filter(self):
        source = PLAYGROUND.read_text(encoding="utf-8")

        self.assertIn("refreshSessions", source)
        self.assertIn("q: historyQuery.value.trim()", source)
        self.assertIn("historyTimeBounds(historyTimeFilter.value)", source)
        self.assertIn("from: bounds.from", source)
        self.assertIn("to: bounds.to", source)
        self.assertNotIn("const historyFilterStartIso = computed", source)
        self.assertNotIn("const historyFilterEndIso = computed", source)
        self.assertIn("const HISTORY_PAGE_SIZE = 30", source)
        self.assertIn("limit: HISTORY_PAGE_SIZE", source)
        self.assertIn("setTimeout", source)
        self.assertIn("clearTimeout(historyFilterTimer)", source)
        schedule_start = source.index("function scheduleHistoryRefresh()")
        schedule_end = source.index("watch([historyQuery, historyTimeFilter]", schedule_start)
        schedule = source[schedule_start:schedule_end]
        self.assertLess(
            schedule.index("invalidateSessionRequests()"),
            schedule.index("setTimeout"),
        )

    def test_time_filter_sends_an_upper_bound_for_future_sessions(self):
        source = PLAYGROUND.read_text(encoding="utf-8")
        store = STORE.read_text(encoding="utf-8")

        self.assertIn("const bounds = historyTimeBounds(historyTimeFilter.value)", source)
        self.assertIn("to: bounds.to", source)
        self.assertNotIn("delete query.to", store)
        self.assertIn("{ queryKey: historyServerQueryKey(params) }", source)

    def test_playground_does_not_offer_page_local_sorting_as_global_history_sorting(self):
        source = PLAYGROUND.read_text(encoding="utf-8")

        self.assertNotIn("time_asc", source)
        self.assertNotIn("prompt_asc", source)
        self.assertNotIn("prompt_desc", source)

    def test_playground_guards_async_history_recall_and_draft_restore(self):
        source = PLAYGROUND.read_text(encoding="utf-8")

        self.assertIn("createHistoryInteractionGuard", source)
        self.assertIn("const token = beginHistoryInteraction()", source)
        self.assertIn("return isCurrentHistoryEntry(resolved, token) ? resolved : null", source)
        self.assertIn("function isCurrentHistoryEntry(entry, token)", source)
        self.assertIn("Boolean(findHistoryEntry(entry.id))", source)
        self.assertIn("if (!isCurrentHistoryEntry(entry, token)) return false", source)
        self.assertIn("historyInteraction.invalidate()", source)
        self.assertIn("await restoreEditDraftForEntry(entry, token)", source)

    def test_region_editor_invalidates_async_draft_restore_before_drawing(self):
        source = REGION_EDITOR.read_text(encoding="utf-8")

        self.assertIn("let restoreGeneration = 0", source)
        self.assertIn("const token = ++restoreGeneration", source)
        self.assertIn("if (token !== restoreGeneration) return false", source)
        self.assertIn("restoreGeneration += 1", source)

        read_start = source.index("function readImageFile(file)")
        read_end = source.index("function initMaskCanvas()", read_start)
        read_image = source[read_start:read_end]
        self.assertIn("const token = ++restoreGeneration", read_image)
        self.assertIn("if (token !== restoreGeneration) return", read_image)

    def test_gallery_uses_shared_store_and_stable_mapped_images(self):
        source = GALLERY.read_text(encoding="utf-8")

        self.assertNotIn("import { listSessions }", source)
        self.assertNotIn("const sessions = ref", source)
        self.assertIn("useGenerationHistory", source)
        self.assertIn(":key=\"item.key\"", source)
        self.assertIn("initialLoading", source)
        self.assertIn("loadingMore", source)
        self.assertIn("retryGalleryLoad", source)
        self.assertIn("loadError.message", source)

    def test_module_state_survives_app_v_if_remounts(self):
        store = STORE.read_text(encoding="utf-8")
        app = APP.read_text(encoding="utf-8")

        self.assertIn("const history = ref", store)
        self.assertIn('<Playground v-if="activePage === \'playground\'"', app)
        self.assertIn("<Gallery v-else", app)

    def test_remounts_reuse_loaded_query_pages_and_explicit_refresh_stays_forceful(self):
        store = STORE.read_text(encoding="utf-8")
        playground = PLAYGROUND.read_text(encoding="utf-8")
        gallery = GALLERY.read_text(encoding="utf-8")

        self.assertIn("ensureSessions", store)
        self.assertIn("loadedQueryKey", store)
        self.assertIn("loadedSuccessfully", store)
        self.assertIn(
            "await ensureSessions(params, { queryKey: historyServerQueryKey(params) })",
            playground,
        )
        self.assertIn("await ensureSessions({})", gallery)
        self.assertIn("@click=\"loadGallery\"", gallery)
        self.assertIn("await refreshSessions()", gallery)


if __name__ == "__main__":
    main()
