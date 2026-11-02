import { useEffect, useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { bridge } from "@/lib/bridge";
import { cn } from "@/lib/utils";
import type {
  AccountSummary,
  AppSettingField,
  AppSettingGroup,
  AppSettingsStatus,
  AppSettingSource,
  ConnectionCheckResult,
} from "@/types";

interface GroupConfig {
  id: AppSettingGroup;
  title: string;
  description: string;
  /** Keys that must all be set for the group to count as ready. */
  readyKeys: string[];
  optional: boolean;
  checkService?: "apify" | "cloudflare";
}

const GROUPS: GroupConfig[] = [
  {
    id: "apify",
    title: "Apify",
    description: "Scrapes Instagram source profiles. Required for the Scraping tab.",
    readyKeys: ["APIFY_TOKEN"],
    optional: false,
    checkService: "apify",
  },
  {
    id: "cloudflare",
    title: "Cloudflare publisher",
    description:
      "Publishes Reels through your own Worker and the Instagram Graph API. Deploy the Worker first (docs/SETUP.md), then paste its URL and API key here.",
    readyKeys: ["CLOUDFLARE_PUBLISHER_URL", "CLOUDFLARE_PUBLISHER_API_KEY"],
    optional: true,
    checkService: "cloudflare",
  },
  {
    id: "ai",
    title: "AI drafts",
    description: "Direct title and caption generation. The copy-prompt chat path works without any key.",
    readyKeys: ["GROQ_API_KEY"],
    optional: true,
  },
  {
    id: "instagram",
    title: "Instagram sourcing",
    description: "Advanced. Leave empty to use the built-in defaults.",
    readyKeys: ["NICHEFLOW_IG_SOURCING_PROFILES"],
    optional: true,
  },
];

const SOURCE_LABEL: Record<AppSettingSource, string> = {
  app: "Saved in Settings",
  env_file: "From the .env file",
  system: "From a system environment variable. It overrides anything saved here.",
};

type Drafts = Record<string, string>;

function initialDraft(field: AppSettingField): string {
  // Secret inputs start empty: an empty secret input means "keep the saved key".
  if (field.secret) return "";
  // Re-serialize so an untouched map compares equal to what the editor emits.
  if (field.kind === "account_map") return serializeAccountMap(parseAccountMap(field.value ?? ""));
  return field.value ?? "";
}

function parseAccountMap(raw: string): Record<string, string> {
  if (!raw.trim()) return {};
  try {
    const data: unknown = JSON.parse(raw);
    if (data && typeof data === "object" && !Array.isArray(data)) {
      return Object.fromEntries(
        Object.entries(data as Record<string, unknown>).map(([id, key]) => [id, String(key ?? "")]),
      );
    }
  } catch {
    // An unreadable map shows as empty rows; saving rewrites it cleanly.
  }
  return {};
}

function serializeAccountMap(map: Record<string, string>): string {
  const entries = Object.entries(map)
    .map(([id, key]) => [id, key.trim()] as const)
    .filter(([, key]) => key !== "");
  return entries.length ? JSON.stringify(Object.fromEntries(entries)) : "";
}

interface SettingsScreenProps {
  accounts: AccountSummary[];
}

export function SettingsScreen({ accounts }: SettingsScreenProps) {
  const [status, setStatus] = useState<AppSettingsStatus | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Bumped when a group saves, which remounts that card so its inputs restart
  // from the fresh status. Other cards keep their unsaved edits.
  const [saveCounts, setSaveCounts] = useState<Partial<Record<AppSettingGroup, number>>>({});

  const handleSaved = (groupId: AppSettingGroup, next: AppSettingsStatus) => {
    setStatus(next);
    setSaveCounts((prev) => ({ ...prev, [groupId]: (prev[groupId] ?? 0) + 1 }));
  };

  useEffect(() => {
    let cancelled = false;
    bridge.getAppSettings().then(
      (next) => {
        if (!cancelled) setStatus(next);
      },
      (err: unknown) => {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : String(err));
      },
    );
    return () => {
      cancelled = true;
    };
  }, []);

  if (loadError) {
    return <p className="p-6 text-sm text-destructive">Could not load settings: {loadError}</p>;
  }
  if (!status) {
    return <p className="p-6 text-sm text-muted-foreground">Loading settings…</p>;
  }

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4 p-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold">Settings</h1>
        <p className="text-sm text-muted-foreground">
          Keys are stored only on this computer, in{" "}
          <code className="rounded bg-muted px-1 py-0.5 text-xs">{status.settings_path}</code>. Saved
          keys take effect immediately. Secret values are never shown again after saving.
        </p>
      </header>
      {GROUPS.map((group) => (
        <SettingsGroupCard
          key={`${group.id}-${saveCounts[group.id] ?? 0}`}
          group={group}
          fields={status.fields.filter((f) => f.group === group.id)}
          accounts={accounts}
          onSaved={(next) => handleSaved(group.id, next)}
        />
      ))}
    </div>
  );
}

interface SettingsGroupCardProps {
  group: GroupConfig;
  fields: AppSettingField[];
  accounts: AccountSummary[];
  onSaved: (status: AppSettingsStatus) => void;
}

function SettingsGroupCard({ group, fields, accounts, onSaved }: SettingsGroupCardProps) {
  const originals = useMemo(
    () => Object.fromEntries(fields.map((f) => [f.key, initialDraft(f)])) as Drafts,
    [fields],
  );
  const [drafts, setDrafts] = useState<Drafts>(originals);
  const [clears, setClears] = useState<ReadonlySet<string>>(new Set());
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [check, setCheck] = useState<ConnectionCheckResult | null>(null);
  const [checking, setChecking] = useState(false);

  const changes = useMemo(() => {
    const out: Drafts = {};
    for (const field of fields) {
      if (clears.has(field.key)) out[field.key] = "";
      else if (drafts[field.key] !== originals[field.key]) out[field.key] = drafts[field.key];
    }
    return out;
  }, [fields, drafts, clears, originals]);
  const hasChanges = Object.keys(changes).length > 0;
  const ready = group.readyKeys.every((key) => fields.find((f) => f.key === key)?.is_set);

  const setDraft = (key: string, value: string) => {
    setDrafts((prev) => ({ ...prev, [key]: value }));
    setClears((prev) => {
      if (!prev.has(key)) return prev;
      const next = new Set(prev);
      next.delete(key);
      return next;
    });
  };

  const requestClear = (key: string) => {
    setDrafts((prev) => ({ ...prev, [key]: "" }));
    setClears((prev) => new Set(prev).add(key));
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    setCheck(null);
    try {
      onSaved(await bridge.saveAppSettings(changes));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  const runCheck = async () => {
    if (!group.checkService) return;
    setChecking(true);
    setCheck(null);
    try {
      setCheck(await bridge.checkAppConnection(group.checkService));
    } catch (err: unknown) {
      setCheck({ ok: false, message: err instanceof Error ? err.message : String(err) });
    } finally {
      setChecking(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-3">
          <CardTitle>{group.title}</CardTitle>
          <GroupBadge ready={ready} optional={group.optional} />
        </div>
        <CardDescription>{group.description}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {fields.map((field) => (
          <FieldRow
            key={field.key}
            field={field}
            draft={drafts[field.key] ?? ""}
            clearing={clears.has(field.key)}
            accounts={accounts}
            onChange={(value) => setDraft(field.key, value)}
            onClear={() => requestClear(field.key)}
          />
        ))}
        <div className="flex flex-wrap items-center gap-2">
          <Button size="sm" onClick={save} disabled={!hasChanges || saving}>
            {saving ? "Saving…" : "Save"}
          </Button>
          {group.checkService && (
            <Button size="sm" variant="outline" onClick={runCheck} disabled={checking || hasChanges}>
              {checking ? "Checking…" : "Check connection"}
            </Button>
          )}
          {hasChanges && group.checkService && (
            <span className="text-xs text-muted-foreground">Save first, then check.</span>
          )}
        </div>
        {error && <p className="text-sm text-destructive">{error}</p>}
        {check && (
          <p className={cn("text-sm", check.ok ? "text-emerald-500" : "text-destructive")}>
            {check.message}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function GroupBadge({ ready, optional }: { ready: boolean; optional: boolean }) {
  if (ready) return <Badge>Set up</Badge>;
  if (optional) return <Badge variant="outline">Optional</Badge>;
  return <Badge variant="destructive">Needs a key</Badge>;
}

interface FieldRowProps {
  field: AppSettingField;
  draft: string;
  clearing: boolean;
  accounts: AccountSummary[];
  onChange: (value: string) => void;
  onClear: () => void;
}

function FieldRow({ field, draft, clearing, accounts, onChange, onClear }: FieldRowProps) {
  const inputId = `setting-${field.key}`;
  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={inputId} className="text-sm font-medium">
        {field.label}
      </label>
      {field.kind === "account_map" ? (
        <AccountMapInput value={draft} accounts={accounts} onChange={onChange} />
      ) : (
        <div className="flex gap-2">
          <Input
            id={inputId}
            type={field.secret ? "password" : "text"}
            autoComplete="off"
            spellCheck={false}
            value={draft}
            placeholder={secretPlaceholder(field, clearing)}
            onChange={(e) => onChange(e.target.value)}
          />
          {field.secret && field.is_set && !clearing && (
            <Button size="sm" variant="ghost" className="h-9" onClick={onClear}>
              Clear
            </Button>
          )}
        </div>
      )}
      <p className="text-xs text-muted-foreground">{field.help}</p>
      {field.source && (
        <p className={cn("text-xs", field.source === "system" ? "text-amber-500" : "text-muted-foreground")}>
          {SOURCE_LABEL[field.source]}
        </p>
      )}
    </div>
  );
}

function secretPlaceholder(field: AppSettingField, clearing: boolean): string | undefined {
  if (!field.secret) return undefined;
  if (clearing) return "Will be removed when you save";
  if (field.is_set) return `Saved (${field.hint ?? "set"}). Type a new key to replace it.`;
  return "Paste your key";
}

interface AccountMapInputProps {
  value: string;
  accounts: AccountSummary[];
  onChange: (value: string) => void;
}

function AccountMapInput({ value, accounts, onChange }: AccountMapInputProps) {
  const map = parseAccountMap(value);
  if (accounts.length === 0) {
    return <p className="text-sm text-muted-foreground">Add an account on the Accounts tab first.</p>;
  }
  const update = (accountId: number, workerKey: string) => {
    onChange(serializeAccountMap({ ...map, [String(accountId)]: workerKey }));
  };
  return (
    <div className="flex flex-col gap-2 rounded-md border border-border p-3">
      {accounts.map((account) => (
        <div key={account.id} className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] items-center gap-3">
          <span className="truncate text-sm">
            {account.name}
            {account.instagram_handle && (
              <span className="text-muted-foreground"> · @{account.instagram_handle.replace(/^@/, "")}</span>
            )}
          </span>
          <Input
            aria-label={`Worker account key for ${account.name}`}
            autoComplete="off"
            spellCheck={false}
            placeholder="Not cloud-published"
            value={map[String(account.id)] ?? ""}
            onChange={(e) => update(account.id, e.target.value)}
          />
        </div>
      ))}
    </div>
  );
}
