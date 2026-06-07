import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { bridge } from "@/lib/bridge";
import type { AccountSummary, LibraryItem } from "@/types";

export function LibraryScreen() {
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [libraryItems, accountList] = await Promise.all([
        bridge.listLibraryItems(),
        bridge.listAccounts(),
      ]);
      setItems(libraryItems);
      setAccounts(accountList);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const reassign = async (itemId: number, accountId: number | null) => {
    setBusyId(itemId);
    setError(null);
    setMessage(null);
    try {
      await bridge.assignAccount(itemId, accountId);
      await refresh();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (item: LibraryItem) => {
    if (!window.confirm(`Remove "${item.title ?? item.source_url}" from the library?`)) return;
    setBusyId(item.id);
    setError(null);
    setMessage(null);
    try {
      await bridge.removeLibraryItem(item.id);
      await refresh();
      setMessage("Item removed.");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusyId(null);
    }
  };

  const openFolder = async (itemId: number) => {
    try {
      await bridge.openItemFolder(itemId);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="mx-auto max-w-5xl space-y-3 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Library</h1>
        <Button size="sm" variant="secondary" onClick={refresh}>
          Refresh
        </Button>
      </div>
      <p className="text-sm text-muted-foreground">
        Downloaded clips. Acquiring new clips still happens in the desktop app; here you can
        reassign an account, open the file, or remove an item.
      </p>

      {error && <p className="text-sm text-destructive">{error}</p>}
      {message && <p className="text-sm text-emerald-600">{message}</p>}

      {items.length === 0 ? (
        <Card>
          <CardContent className="p-6 text-sm text-muted-foreground">
            No downloaded items yet.
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-2">
          {items.map((item) => (
            <Card key={item.id}>
              <CardContent className="flex flex-wrap items-center gap-3 p-3">
                <div className="min-w-0 grow">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm font-medium">
                      #{item.id} {item.title ?? item.source_url}
                    </span>
                    <Badge variant="outline">{item.status}</Badge>
                    {item.has_processed && <Badge>exported</Badge>}
                    {item.has_draft && !item.has_processed && (
                      <Badge variant="secondary">draft</Badge>
                    )}
                    {!item.has_file && <Badge variant="destructive">no file</Badge>}
                  </div>
                  <div className="truncate text-xs text-muted-foreground">{item.source_url}</div>
                </div>

                <select
                  className="h-9 rounded-md border border-input bg-transparent px-2 text-sm"
                  value={item.account_id ?? ""}
                  disabled={busyId === item.id}
                  onChange={(e) =>
                    reassign(item.id, e.target.value === "" ? null : Number(e.target.value))
                  }
                >
                  <option value="">Unassigned</option>
                  {accounts.map((acc) => (
                    <option key={acc.id} value={acc.id}>
                      {acc.name}
                    </option>
                  ))}
                </select>

                <Button
                  size="sm"
                  variant="outline"
                  disabled={!item.has_file}
                  onClick={() => openFolder(item.id)}
                >
                  Open folder
                </Button>
                <Button
                  size="sm"
                  variant="destructive"
                  disabled={busyId === item.id}
                  onClick={() => remove(item)}
                >
                  Remove
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
