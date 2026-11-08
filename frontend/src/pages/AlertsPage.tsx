// Alerts page: firing and history for everyone; rules for admins. Notification channels
// live in Settings.

import { useUser } from "../app/session";
import { EventsSection } from "./alerts/EventsSection";
import { RulesSection } from "./alerts/RulesSection";
import { PageHeader } from "../components/PageHeader";

export function AlertsPage() {
  const isAdmin = useUser().role === "admin";
  return (
    <div className="space-y-7">
      <PageHeader title="Alerts" />
      <EventsSection />
      {isAdmin && <RulesSection />}
    </div>
  );
}
