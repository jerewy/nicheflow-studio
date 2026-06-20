import { useCallback, useEffect, useState } from "react";

import { AccountManager } from "@/components/AccountManager";
import { Dashboard } from "@/components/Dashboard";
import { ProcessingScreen } from "@/components/ProcessingScreen";
import { PublishEventToaster } from "@/components/PublishEventToaster";
import { ScrapingScreen } from "@/components/ScrapingScreen";
import { ToastProvider } from "@/components/ui/Toast";
import { bridge, whenBridgeReady } from "@/lib/bridge";
import { cn } from "@/lib/utils";
import type { AccountSummary } from "@/types";

type Tab = "accounts" | "dashboard" | "scraping" | "processing";

const TABS: { id: Tab; label: string; gated: boolean }[] = [
  { id: "accounts", label: "Accounts", gated: false },
  { id: "dashboard", label: "Dashboard", gated: true },
  { id: "scraping", label: "Scraping", gated: true },
  { id: "processing", label: "Processing", gated: true },
];

function App() {
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [tab, setTab] = useState<Tab>("accounts");
  const [loaded, setLoaded] = useState(false);
  const [activeAccountError, setActiveAccountError] = useState<string | null>(null);
  // Deep-link target handed to Processing from the publish schedule ("Edit in
  // Processing"). The item id is the reliable key (the exported title differs from
  // the library item's original title); search is a fallback. Cleared on any
  // manual visit so it doesn't linger.
  const [processingItemId, setProcessingItemId] = useState<number | null>(null);
  const [processingSearch, setProcessingSearch] = useState("");

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
    setProcessingItemId(itemId);
    setProcessingSearch(search);
    setTab("processing");
  };

  const activeTabConfig = TABS.find((t) => t.id === tab);
  const effectiveTab: Tab =
    activeId === null && activeTabConfig?.gated ? "accounts" : tab;
  const activeName = accounts.find((a) => a.id === activeId)?.name ?? null;

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
                  if (t.id === "processing") {
                    setProcessingItemId(null);
                    setProcessingSearch("");
                  }
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

      {effectiveTab === "accounts" && (
        <AccountManager
          activeId={activeId}
          onAccountsChanged={refreshAccounts}
          onUseAccount={chooseActive}
        />
      )}
      {effectiveTab === "processing" &&
        (activeId !== null ? (
          <ProcessingScreen
            activeAccountId={activeId}
            activeAccountName={activeName}
            initialItemId={processingItemId}
            initialSearch={processingSearch}
          />
        ) : null)}
      {effectiveTab === "dashboard" && activeId !== null ? (
        <Dashboard activeAccountId={activeId} onOpenInProcessing={openInProcessing} />
      ) : null}
      {effectiveTab === "scraping" && activeId !== null ? (
        <ScrapingScreen activeAccountId={activeId} activeAccountName={activeName} />
      ) : null}

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
