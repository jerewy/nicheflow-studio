import { useState } from "react";

import { AccountManager } from "@/components/AccountManager";
import { ProcessingScreen } from "@/components/ProcessingScreen";
import { cn } from "@/lib/utils";

type Tab = "processing" | "accounts";

const TABS: { id: Tab; label: string }[] = [
  { id: "processing", label: "Processing" },
  { id: "accounts", label: "Accounts" },
];

function App() {
  const [tab, setTab] = useState<Tab>("processing");

  return (
    <main className="dark min-h-screen bg-background text-foreground">
      <nav className="flex items-center gap-1 border-b border-border px-4 py-2">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={cn(
              "rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              tab === t.id
                ? "bg-secondary text-secondary-foreground"
                : "text-muted-foreground hover:bg-accent hover:text-accent-foreground",
            )}
          >
            {t.label}
          </button>
        ))}
      </nav>
      {tab === "processing" ? <ProcessingScreen /> : <AccountManager />}
    </main>
  );
}

export default App;
