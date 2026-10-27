import { EmptyState, PageHeader } from "../components/PageHeader";

export function DashboardPage() {
  return (
    <>
      <PageHeader title="Dashboard" />
      {/* Cards render here: one column on phones, a grid from md up. */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        <EmptyState>No cards yet. Add a device to get started.</EmptyState>
      </div>
    </>
  );
}
