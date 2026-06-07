export function DashboardTable({
  headers,
  children,
  maxHeightClass = "max-h-96",
}: {
  headers: string[];
  children: React.ReactNode;
  /** Tailwind max-height class for the scroll area. Defaults to ~384px. */
  maxHeightClass?: string;
}) {
  return (
    <div className={`${maxHeightClass} overflow-auto rounded-md border border-border`}>
      <table className="w-full text-left text-sm">
        <thead className="bg-muted text-muted-foreground">
          <tr>
            {headers.map((header) => (
              <th
                key={header}
                className="sticky top-0 z-10 whitespace-nowrap bg-muted px-3 py-2 font-medium"
              >
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}
