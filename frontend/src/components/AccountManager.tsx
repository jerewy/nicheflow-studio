import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { bridge } from "@/lib/bridge";
import type {
  AccountDetail,
  AccountOperationalStatus,
  AccountSummary,
  CloudAccountSettings,
} from "@/types";

type FormState = Record<string, string>;

const OPERATIONAL_STATUSES: AccountOperationalStatus[] = ["active", "resting", "flagged"];

const OPERATIONAL_STATUS_BADGE_VARIANT: Record<
  AccountOperationalStatus,
  "default" | "secondary" | "destructive"
> = {
  active: "default",
  resting: "secondary",
  flagged: "destructive",
};

// Editable fields sent to the backend (credential_blob is intentionally omitted
// from the UI; a partial update preserves it).
const SINGLE_LINE: { key: string; label: string; placeholder?: string }[] = [
  { key: "name", label: "Name" },
  { key: "platform", label: "Platform", placeholder: "instagram" },
  { key: "niche", label: "Strict niche (pooling)", placeholder: "history | movie" },
  { key: "instagram_handle", label: "Instagram handle", placeholder: "@handle" },
  { key: "instagram_profile", label: "Instagram profile (login slot)" },
  { key: "login_identifier", label: "Login identifier" },
  { key: "upload_timezone", label: "Upload timezone", placeholder: "Asia/Jakarta" },
  { key: "upload_default_privacy", label: "Default privacy", placeholder: "private" },
  { key: "upload_schedule_slots", label: "Schedule slots", placeholder: "09:00, 18:00" },
  { key: "daily_posts_target", label: "Daily posts target", placeholder: "Default: 4" },
  { key: "distribute_daily_target", label: "Distribute per day (backlog)", placeholder: "Default: 5" },
];

const MULTI_LINE: { key: string; label: string }[] = [
  { key: "niche_label", label: "Niche description" },
  { key: "writing_tone", label: "Writing tone" },
  { key: "target_audience", label: "Target audience" },
  { key: "hook_style", label: "Hook style" },
  { key: "banned_phrases", label: "Banned phrases" },
  { key: "title_style_notes", label: "Title style notes" },
  { key: "caption_style_notes", label: "Caption style notes" },
];

const ALL_KEYS = [...SINGLE_LINE, ...MULTI_LINE].map((f) => f.key);

const EMPTY_FORM: FormState = {
  ...(Object.fromEntries(ALL_KEYS.map((k) => [k, ""])) as FormState),
  operational_status: "active",
};

function detailToForm(detail: AccountDetail): FormState {
  const form: FormState = { ...EMPTY_FORM };
  for (const key of ALL_KEYS) {
    const value = (detail as unknown as Record<string, unknown>)[key];
    form[key] = value == null ? "" : String(value);
  }
  form.operational_status = detail.operational_status ?? "active";
  return form;
}

// Worker-side safety caps for a cloud-mapped account (daily_limit / min_gap_minutes,
// the mechanism that gated resurfacedhistory's 18:00 slot). Renders nothing for an
// account that isn't cloud-mapped or hasn't been registered on the Worker yet.
function CloudSafetyPanel({ accountId }: { accountId: number }) {
  const [settings, setSettings] = useState<CloudAccountSettings | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [dailyLimit, setDailyLimit] = useState("");
  const [minGapMinutes, setMinGapMinutes] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoaded(false);
    setError(null);
    setSaved(false);
    bridge
      .dashboardCloudAccountSettings(accountId)
      .then((result) => {
        if (cancelled) return;
        setSettings(result);
        if (result) {
          setDailyLimit(String(result.daily_limit));
          setMinGapMinutes(String(result.min_gap_minutes));
          setEnabled(result.enabled);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, [accountId]);

  if (!loaded || !settings) return null;

  const save = async () => {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      const updated = await bridge.dashboardUpdateCloudAccountSettings(
        accountId,
        Number(dailyLimit),
        Number(minGapMinutes),
        enabled,
      );
      setSettings(updated);
      setSaved(true);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2 rounded-md border border-border p-3">
      <p className="text-sm font-medium">Cloud publish safety</p>
      <p className="text-xs text-muted-foreground">
        Enforced by the Cloudflare Worker on every post — the anti-flagging guard, not
        a local scheduling preference. Raising these increases automation-flag risk.
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">
            Daily limit (1–20)
          </label>
          <Input
            type="number"
            min={1}
            max={20}
            value={dailyLimit}
            onChange={(e) => setDailyLimit(e.target.value)}
          />
        </div>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">
            Min gap between posts (minutes, ≥30)
          </label>
          <Input
            type="number"
            min={30}
            value={minGapMinutes}
            onChange={(e) => setMinGapMinutes(e.target.value)}
          />
        </div>
      </div>
      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(event) => setEnabled(event.target.checked)}
        />
        Cloud publishing enabled for this account
      </label>
      {error && <p className="text-xs text-destructive">{error}</p>}
      {saved && !error && <p className="text-xs text-emerald-600">Saved.</p>}
      <Button size="sm" variant="outline" onClick={save} disabled={busy}>
        Save cloud safety settings
      </Button>
    </div>
  );
}

interface AccountManagerProps {
  activeId: number | null;
  onAccountsChanged?: () => void;
  onUseAccount?: (id: number) => void;
}

export function AccountManager({ activeId, onAccountsChanged, onUseAccount }: AccountManagerProps) {
  const [accounts, setAccounts] = useState<AccountSummary[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [detail, setDetail] = useState<AccountDetail | null>(null);
  const [autoScheduleOnExport, setAutoScheduleOnExport] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refreshList = useCallback(async () => {
    try {
      setAccounts(await bridge.listAccounts());
      onAccountsChanged?.();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [onAccountsChanged]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void refreshList();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [refreshList]);

  const selectAccount = useCallback(async (id: number) => {
    setError(null);
    setMessage(null);
    setCreating(false);
    try {
      const d = await bridge.getAccount(id);
      setDetail(d);
      setForm(detailToForm(d));
      setAutoScheduleOnExport(d.auto_schedule_on_export);
      setSelectedId(id);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  // Default the panel to the active account's profile, so opening Accounts shows
  // the current account instead of an empty selector. Only kicks in when an
  // active account exists and nothing is already being viewed or created.
  useEffect(() => {
    if (activeId === null || selectedId !== null || creating) return;
    const timer = window.setTimeout(() => selectAccount(activeId), 0);
    return () => window.clearTimeout(timer);
  }, [activeId, selectedId, creating, selectAccount]);

  const startNew = () => {
    setError(null);
    setMessage(null);
    setCreating(true);
    setSelectedId(null);
    setDetail(null);
    setForm({ ...EMPTY_FORM, platform: "instagram" });
    setAutoScheduleOnExport(false);
  };

  const setField = (key: string, value: string) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const save = async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const payload: Record<string, unknown> = {
        ...form,
        auto_schedule_on_export: autoScheduleOnExport,
      };
      if (creating) {
        const created = await bridge.createAccount(payload);
        await refreshList();
        await selectAccount(created.id);
        setMessage("Account created.");
      } else if (selectedId !== null) {
        const updated = await bridge.updateAccount(selectedId, payload);
        setDetail(updated);
        setForm(detailToForm(updated));
        setAutoScheduleOnExport(updated.auto_schedule_on_export);
        await refreshList();
        setMessage("Account saved.");
      }
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (selectedId === null) return;
    const counts = detail
      ? ` This unassigns ${detail.download_item_count} clip(s) and removes ${detail.upload_job_count} queue job(s).`
      : "";
    if (!window.confirm(`Delete this account?${counts}`)) return;
    setBusy(true);
    setError(null);
    try {
      await bridge.deleteAccount(selectedId);
      setSelectedId(null);
      setDetail(null);
      setForm(EMPTY_FORM);
      setCreating(false);
      await refreshList();
      setMessage("Account deleted.");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const editing = creating || selectedId !== null;

  return (
    <div className="mx-auto grid max-w-5xl gap-4 p-6 md:grid-cols-[260px_1fr]">
      <aside className="space-y-2">
        <div className="flex items-center justify-between">
          <h1 className="text-lg font-semibold tracking-tight">Accounts</h1>
          <Button size="sm" onClick={startNew}>
            New
          </Button>
        </div>
        <div className="space-y-1">
          {accounts.map((acc) => (
            <div
              key={acc.id}
              className={cn(
                "rounded-md border px-3 py-2 text-sm transition-colors",
                selectedId === acc.id && "border-ring",
                activeId === acc.id && "bg-accent",
              )}
            >
              <button
                type="button"
                onClick={() => selectAccount(acc.id)}
                className="block w-full text-left"
              >
                <div className="flex items-center gap-2 font-medium">
                  {acc.name}
                  {activeId === acc.id && (
                    <span className="text-[10px] font-semibold text-emerald-500">● ACTIVE</span>
                  )}
                  {acc.operational_status !== "active" && (
                    <Badge
                      variant={OPERATIONAL_STATUS_BADGE_VARIANT[acc.operational_status]}
                      className="px-1.5 py-0 text-[10px] uppercase"
                    >
                      {acc.operational_status}
                    </Badge>
                  )}
                </div>
                <div className="text-xs text-muted-foreground">
                  {acc.platform}
                  {acc.niche_label ? ` · ${acc.niche_label}` : ""}
                </div>
              </button>
              {activeId !== acc.id && onUseAccount && (
                <button
                  type="button"
                  onClick={() => onUseAccount(acc.id)}
                  className="mt-1 text-xs text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
                >
                  Use as active niche
                </button>
              )}
            </div>
          ))}
          {accounts.length === 0 && (
            <p className="text-sm text-muted-foreground">No accounts yet.</p>
          )}
        </div>
      </aside>

      <section className="space-y-3">
        {error && <p className="text-sm text-destructive">{error}</p>}
        {message && <p className="text-sm text-emerald-600">{message}</p>}

        {!editing ? (
          <Card>
            <CardContent className="p-6 text-sm text-muted-foreground">
              Select an account on the left, or click New to create one.
            </CardContent>
          </Card>
        ) : (
          <Card>
            <CardHeader className="flex-row items-center justify-between space-y-0">
              <CardTitle className="text-base">
                {creating ? "New account" : `Editing: ${detail?.name ?? ""}`}
              </CardTitle>
              {detail && (
                <span className="text-xs text-muted-foreground">
                  {detail.download_item_count} clips · {detail.upload_job_count} queue jobs
                </span>
              )}
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="space-y-1">
                <label className="text-xs font-medium text-muted-foreground">
                  Operational status
                </label>
                <select
                  className="flex h-9 w-full max-w-xs rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-sm transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  value={form.operational_status || "active"}
                  onChange={(e) => setField("operational_status", e.target.value)}
                >
                  {OPERATIONAL_STATUSES.map((status) => (
                    <option key={status} value={status}>
                      {status}
                    </option>
                  ))}
                </select>
                <p className="text-xs text-muted-foreground">
                  Resting/flagged accounts are skipped by distribution and publish
                  scheduling without losing their history.
                </p>
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                {SINGLE_LINE.map((f) => (
                  <div key={f.key} className="space-y-1">
                    <label className="text-xs font-medium text-muted-foreground">
                      {f.label}
                    </label>
                    <Input
                      value={form[f.key] ?? ""}
                      placeholder={f.placeholder}
                      onChange={(e) => setField(f.key, e.target.value)}
                    />
                  </div>
                ))}
              </div>
              <div className="grid gap-3 sm:grid-cols-2">
                {MULTI_LINE.map((f) => (
                  <div key={f.key} className="space-y-1">
                    <label className="text-xs font-medium text-muted-foreground">
                      {f.label}
                    </label>
                    <Textarea
                      rows={2}
                      value={form[f.key] ?? ""}
                      onChange={(e) => setField(f.key, e.target.value)}
                    />
                  </div>
                ))}
              </div>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={autoScheduleOnExport}
                  onChange={(event) => setAutoScheduleOnExport(event.target.checked)}
                />
                Auto-schedule each reel after export
              </label>
              {!creating && selectedId !== null && <CloudSafetyPanel accountId={selectedId} />}
              <div className="flex items-center gap-2">
                <Button onClick={save} disabled={busy}>
                  {creating ? "Create account" : "Save changes"}
                </Button>
                {!creating && selectedId !== null && (
                  <Button variant="destructive" onClick={remove} disabled={busy}>
                    Delete
                  </Button>
                )}
              </div>
            </CardContent>
          </Card>
        )}
      </section>
    </div>
  );
}
