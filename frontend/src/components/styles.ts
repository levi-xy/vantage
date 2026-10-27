// Tailwind class strings shared by forms and buttons.

const focusRing = "focus-visible:ring-2 focus-visible:ring-sky-500 focus-visible:outline-none";

export const buttonPrimary = `inline-flex h-10 items-center justify-center rounded-md bg-sky-700 px-4 text-sm font-medium text-white hover:bg-sky-800 disabled:cursor-not-allowed disabled:opacity-60 dark:bg-sky-600 dark:hover:bg-sky-500 ${focusRing}`;

export const buttonSecondary = `inline-flex h-9 items-center justify-center rounded-md border border-slate-300 px-3 text-sm text-slate-700 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-60 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800 ${focusRing}`;

export const input = `block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 aria-[invalid=true]:border-red-600 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-100 dark:aria-[invalid=true]:border-red-400 ${focusRing}`;

export const label = "mb-1 block text-sm font-medium";

export const fieldHint = "mt-1 text-xs text-slate-600 dark:text-slate-400";

export const fieldError = "mt-1 text-xs text-red-700 dark:text-red-400";

export const alertError =
  "rounded-md border border-red-300 bg-red-50 px-3 py-2 text-sm text-red-800 dark:border-red-800 dark:bg-red-950 dark:text-red-200";

export const alertInfo =
  "rounded-md border border-sky-300 bg-sky-50 px-3 py-2 text-sm text-sky-900 dark:border-sky-800 dark:bg-sky-950 dark:text-sky-100";
