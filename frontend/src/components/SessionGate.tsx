import { FlaskConical, Radar } from "lucide-react";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { Card, EmptyState, Skeleton } from "@/components/ui";
import { sessionStore } from "@/services/session";
import { useStore } from "@/services/store";

/** Renders children only when a session is selected and its state has loaded. */
export function SessionGate({ children, what = "this view" }: { children: ReactNode; what?: string }) {
  const activeId = useStore(sessionStore, (s) => s.activeId);
  const hasSnapshot = useStore(sessionStore, (s) => s.snapshot !== null);
  const connection = useStore(sessionStore, (s) => s.connection);
  const error = useStore(sessionStore, (s) => s.error);
  if (!activeId) {
    return (
      <Card>
        <EmptyState
          icon={<Radar size={22} />}
          title="No monitoring session selected"
          body={<>Launch a real pthread program from the Test Lab, or pick an earlier session from the switcher above, to populate {what}.</>}
          action={
            <Link to="/app/lab" className="btn btn-primary">
              <FlaskConical size={15} /> Open Test Lab
            </Link>
          }
        />
      </Card>
    );
  }
  if (connection === "error" && !hasSnapshot) {
    return (
      <Card>
        <EmptyState icon={<Radar size={22} />} title="Could not load the session" body={error ?? "The backend did not respond."} />
      </Card>
    );
  }
  if (!hasSnapshot) {
    return (
      <div className="space-y-4" aria-busy="true">
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {Array.from({ length: 4 }, (_, i) => (
            <Skeleton key={i} className="h-24" />
          ))}
        </div>
        <Skeleton className="h-72" />
        <p className="text-center text-sm text-fg-3">Waiting for the first events from the collector…</p>
      </div>
    );
  }
  return <>{children}</>;
}
