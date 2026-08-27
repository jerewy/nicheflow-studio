import { useCallback, useEffect, useRef, useState } from "react";

import { AccountManager } from "@/components/AccountManager";
import { Dashboard } from "@/components/Dashboard";
import { ProcessingScreen, type ProcessingDeepLink } from "@/components/ProcessingScreen";
import { PublishEventToaster } from "@/components/PublishEventToaster";
import ClipStudioScreen from "@/components/ClipStudioScreen";
import { ScrapingScreen } from "@/components/ScrapingScreen";
import { ToastProvider } from "@/components/ui/Toast";
import { usePauseHiddenMedia } from "@/hooks/useKeepAlive";
import { bridge, whenBridgeReady } from "@/lib/bridge";
import { cn } from "@/lib/utils";
import type { AccountSummary } from "@/types";

type Tab = "accounts" | "dashboard" | "scraping" | "processing" | "clipstudio";

const TABS: { id: Tab; label: string; gated: boolean }[] = [
  { id: "accounts", label: "Accounts", gated: false },
  { id: "dashboard", label: "Dashboard", gated: true },
  { id: "scraping", label: "Scraping", gated: true },
  { id: "processing", label: "Processing", gated: true },
  // Campaign clipping runs on dedicated clip accounts, so it is not gated on
  // the active niche account the way the network screens are.
  { id: "clipstudio", label: "Clip Studio", gated: false },
];

function App() {
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [tab, setTab] = useState<Tab>("accounts");
  const [loaded, setLoaded] = useState(false);
  const [activeAccountError, setActiveAccountError] = useState<string | null>(null);
  // Deep-link target handed to Processing from the publish schedule ("Edit in
  // Processing"). A fresh object per navigation so re-linking the same item still
  // re-applies the pin; cleared (null) on any manual visit so it doesn't linger.
  const [processingLink, setProcessingLink] = useState<ProcessingDeepLink | null>(null);
  // Screens stay mounted once visited (hidden with CSS when inactive) so
  // background jobs — exports, scheduling, publishing, scrapes — keep their
  // progress UI and timers across tab switches. Lazy: a tab mounts on first
  // visit, not at startup. Updated from the navigation handlers, the only
  // places the tab can change.
  const [visitedTabs, setVisitedTabs] = useState<ReadonlySet<Tab>>(() => new Set(["accounts"]));
  const tabsHostRef = useRef<HTMLDivElement | null>(null);

  const markVisited = (t: Tab) =>
    setVisitedTabs((prev) => (prev.has(t) ? prev : new Set(prev).add(t)));

  const refreshAccounts = useCallback(async () => {
    try {
      setAccounts(await bridge.listAccounts());
    } catch {
      setAccounts([]);
    }
  }, []);

  useEffect(() => {
    whenBridgeReady().then(async () => {
      await refreshAccounts();
      try {
        const { active_account_id } = await bridge.getActiveAccount();
        setActiveId(active_account_id);
      } catch {
        setActiveId(null);
      }
      setLoaded(true);
    });
  }, [refreshAccounts]);

  const chooseActive = async (id: number | null) => {
    try {
      const result = await bridge.setActiveAccount(id);
      setActiveId(result.active_account_id);
      setActiveAccountError(null);
    } catch (err: unknown) {
      setActiveAccountError(err instanceof Error ? err.message : String(err));
    }
  };

  // Switch the active niche to the reel's account, then jump to Processing focused
  // on the exact library item so it can be re-edited and re-exported without hunting.
  const openInProcessing = async (
    accountId: number,
    itemId: number | null,
    search: string,
  ) => {
    await chooseActive(accountId);
    setProcessingLink({ itemId, search });
    markVisited("processing");
    setTab("processing");
  };

  const activeTabConfig = TABS.find((t) => t.id === tab);
  const effectiveTab: Tab =
    activeId === null && activeTabConfig?.gated ? "accounts" : tab;
  const activeName = accounts.find((a) => a.id === activeId)?.name ?? null;

  // A hidden tab keeps playing <video> audio otherwise (display:none doesn't pause).
  usePauseHiddenMedia(tabsHostRef, effectiveTab);

  const isTabMounted = (t: Tab) => effectiveTab === t || visitedTabs.has(t);

  return (
    <main className="dark min-h-screen bg-background text-foreground">
      <ToastProvider>
      <PublishEventToaster />
      <nav className="flex items-center justify-between gap-3 border-b border-border px-4 py-2">
        <div className="flex items-center gap-1">
          {TABS.map((t) => {
            const locked = t.gated && activeId === null;
            return (
              <button
                key={t.id}
                onClick={() => {
                  if (locked) return;
                  // A manual visit should not inherit a stale deep-link target.
                  if (t.id === "processing") setProcessingLink(null);
                  markVisited(t.id);
                  setTab(t.id);
                }}
                disabled={locked}
                title={locked ? "Choose an active niche account first" : undefined}
                className={cn(
                  "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
                  effectiveTab === t.id
                    ? "bg-secondary text-secondary-foreground"
                    : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
                  locked && "cursor-not-allowed opacity-40 hover:bg-transparent",
                )}
              >
                {t.label}
                {locked && " 🔒"}
              </button>
            );
          })}
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">Active niche</span>
          <select
            className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
            value={activeId ?? ""}
            onChange={(e) => chooseActive(e.target.value === "" ? null : Number(e.target.value))}
          >
            <option value="">Choose an account…</option>
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </select>
          {activeAccountError && (
            <span className="text-xs text-destructive">{activeAccountError}</span>
          )}
        </div>
      </nav>

      {/* Keep-alive tabs: once mounted, an inactive screen is hidden, not
          unmounted, so in-flight jobs keep reporting progress. Gated screens
          still unmount if the active niche is cleared. */}
      <div ref={tabsHostRef}>
        <div hidden={effectiveTab !== "accounts"}>
          <AccountManager
            activeId={activeId}
            onAccountsChanged={refreshAccounts}
            onUseAccount={chooseActive}
          />
        </div>
        {activeId !== null && isTabMounted("processing") && (
          <div hidden={effectiveTab !== "processing"}>
            <ProcessingScreen
              activeAccountId={activeId}
              activeAccountName={activeName}
              deepLink={processingLink}
              active={effectiveTab === "processing"}
            />
          </div>
        )}
        {activeId !== null && isTabMounted("dashboard") && (
          <div hidden={effectiveTab !== "dashboard"}>
            <Dashboard
              activeAccountId={activeId}
              onOpenInProcessing={openInProcessing}
              active={effectiveTab === "dashboard"}
            />
          </div>
        )}
        {activeId !== null && isTabMounted("scraping") && (
          <div hidden={effectiveTab !== "scraping"}>
            <ScrapingScreen
              activeAccountId={activeId}
              activeAccountName={activeName}
              active={effectiveTab === "scraping"}
            />
          </div>
        )}
        {isTabMounted("clipstudio") && (
          <div hidden={effectiveTab !== "clipstudio"}>
            <ClipStudioScreen active={effectiveTab === "clipstudio"} />
          </div>
        )}
      </div>

      {loaded && activeId === null && effectiveTab === "accounts" && (
        <p className="px-6 pb-6 text-sm text-muted-foreground">
          Tip: choose an <span className="font-medium">active niche</span> (top-right) to unlock
          Processing and the Dashboard.
        </p>
      )}
      </ToastProvider>
    </main>
  );
}

export default App;
