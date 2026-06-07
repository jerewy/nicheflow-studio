import { useState } from "react";

import { AccountManager } from "@/components/AccountManager";
import { LibraryScreen } from "@/components/LibraryScreen";
import { ProcessingScreen } from "@/components/ProcessingScreen";
import { PublishingScreen } from "@/components/PublishingScreen";
import { cn } from "@/lib/utils";

type Tab = "accounts" | "library" | "processing" | "publishing";

// Ordered to match the workflow: account -> library -> process -> publish.
const TABS: { id: Tab; label: string }[] = [
  { id: "accounts", label: "Accounts" },
  { id: "library", label: "Library" },
  { id: "processing", label: "Processing" },
  { id: "publishing", label: "Publishing" },
];

function App() {
  const [tab, setTab] = useState<Tab>("accounts");

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
      {tab === "accounts" && <AccountManager />}
      {tab === "library" && <LibraryScreen />}
      {tab === "processing" && <ProcessingScreen />}
      {tab === "publishing" && <PublishingScreen />}
    </main>
  );
}

export default App;
