import { useCallback, useEffect, useState } from "react";

import { AccountManager } from "@/components/AccountManager";
import { Dashboard } from "@/components/Dashboard";
import { ProcessingScreen } from "@/components/ProcessingScreen";
import { bridge, whenBridgeReady } from "@/lib/bridge";
import { cn } from "@/lib/utils";
import type { AccountSummary } from "@/types";

type Tab = "accounts" | "processing" | "dashboard";

const TABS: { id: Tab; label: string; gated: boolean }[] = [
  { id: "accounts", label: "Accounts", gated: false },
  { id: "processing", label: "Processing", gated: true },
  { id: "dashboard", label: "Dashboard", gated: true },
];

function App() {
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [tab, setTab] = useState<Tab>("accounts");
  const [loaded, setLoaded] = useState(false);

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
    } catch {
      /* ignore */
    }
  };

  // Hard gate: without an active account, only Accounts is usable.
  const effectiveTab: Tab = activeId === null && tab !== "accounts" ? "accounts" : tab;
  const activeName = accounts.find((a) => a.id === activeId)?.name ?? null;

  return (
    <main className="dark min-h-screen bg-background text-foreground">
      <nav className="flex items-center justify-between gap-3 border-b border-border px-4 py-2">
        <div className="flex items-center gap-1">
          {TABS.map((t) => {
            const locked = t.gated && activeId === null;
            return (
              <button
                key={t.id}
                onClick={() => !locked && setTab(t.id)}
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
          <ProcessingScreen activeAccountId={activeId} activeAccountName={activeName} />
        ) : null)}
      {effectiveTab === "dashboard" && activeId !== null ? <Dashboard /> : null}

      {loaded && activeId === null && effectiveTab === "accounts" && (
        <p className="px-6 pb-6 text-sm text-muted-foreground">
          Tip: choose an <span className="font-medium">active niche</span> (top-right) to unlock
          Processing and the Dashboard.
        </p>
      )}
    </main>
  );
}

export default App;
