import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

interface OptionCardProps {
  optionNumber: number; // 1-based
  title: string;
  caption: string;
  note?: string;
  tier?: string;
  recommended?: boolean;
  applied?: boolean;
  busy?: boolean;
  onTitleChange: (value: string) => void;
  onCaptionChange: (value: string) => void;
  onApply: () => void;
}

const tierClass: Record<string, string> = {
  green: "border-l-emerald-500",
  yellow: "border-l-amber-500",
  red: "border-l-rose-500",
};

export function OptionCard({
  optionNumber,
  title,
  caption,
  note,
  tier,
  recommended,
  applied,
  busy,
  onTitleChange,
  onCaptionChange,
  onApply,
}: OptionCardProps) {
  return (
    <Card
      className={cn(
        "border-l-4",
        tier ? tierClass[tier] ?? "border-l-border" : "border-l-border",
        applied && "ring-2 ring-ring",
      )}
    >
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold">Option {optionNumber}</span>
          {recommended && <Badge variant="secondary">Recommended</Badge>}
          {applied && <Badge>Applied</Badge>}
        </div>
        <Button
          size="sm"
          variant={applied ? "secondary" : "default"}
          onClick={onApply}
          disabled={busy}
        >
          {applied ? "Re-apply" : "Apply"}
        </Button>
      </CardHeader>
      <CardContent className="space-y-2">
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">
            Title
          </label>
          <Input
            value={title}
            onChange={(e) => onTitleChange(e.target.value)}
            placeholder="On-screen title"
          />
        </div>
        <div className="space-y-1">
          <label className="text-xs font-medium text-muted-foreground">
            Caption
          </label>
          <Textarea
            value={caption}
            rows={5}
            onChange={(e) => onCaptionChange(e.target.value)}
            placeholder="Caption"
          />
        </div>
        {note && <p className="text-xs text-muted-foreground italic">{note}</p>}
      </CardContent>
    </Card>
  );
}
