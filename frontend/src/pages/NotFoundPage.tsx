import { Link } from "react-router";
import { PageHeader } from "../components/PageHeader";

export function NotFoundPage() {
  return (
    <>
      <PageHeader title="Page not found" />
      <p className="text-sm text-slate-600 dark:text-slate-400">
        Nothing lives at this address.{" "}
        <Link to="/" className="text-sky-700 underline dark:text-sky-400">
          Back to the dashboard
        </Link>
      </p>
    </>
  );
}
