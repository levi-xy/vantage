// Alerts page: firing and history for everyone; rules, templates and channels for admins.
//
// The bot token is write-only: its input is a password field that is never pre-filled
// and is cleared after every submit; the page shows only whether a token is set.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { FormEvent } from "react";
import { ApiError } from "../api";
import type { Channel, ChannelTestResult } from "../api";
import { useApiClient, useUser } from "../app/session";
import { EventsSection } from "./alerts/EventsSection";
import { RulesSection } from "./alerts/RulesSection";
import { EmptyState, PageHeader } from "../components/PageHeader";
import {
  alertError,
  alertInfo,
  buttonPrimary,
  buttonSecondary,
  fieldHint,
  input,
  label,
} from "../components/styles";

const CHANNELS_KEY = ["channels"] as const;
const card =
  "rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900";
const buttonDanger = `${buttonSecondary} border-red-400 text-red-700 dark:border-red-700 dark:text-red-300`;

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 403) return "Only admins can manage alert channels.";
    if (error.status === 422) return `Check the fields: ${error.message}`;
    return error.message;
  }
  return "Something went wrong. Try again.";
}

/** Create (no `channel`) or edit a Telegram channel. */
function ChannelForm({ channel, onDone }: { channel?: Channel; onDone: () => void }) {
  const api = useApiClient();
  const queryClient = useQueryClient();
  const prefix = channel ? `channel-${channel.id}` : "new-channel";
  const [name, setName] = useState(channel?.name ?? "");
  const [chatId, setChatId] = useState(channel?.config.chat_id ?? "");
  // Never pre-filled: the backend never sends the token back.
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const tokenSet = channel?.secrets_set.bot_token === true;

  const save = useMutation({
    mutationFn: () => {
      if (channel) {
        const config: { chat_id: string; bot_token?: string } = { chat_id: chatId.trim() };
        if (token) config.bot_token = token;
        return api.updateChannel(channel.id, { name: name.trim(), config });
      }
      return api.createChannel({
        type: "telegram",
        name: name.trim(),
        config: { chat_id: chatId.trim(), bot_token: token },
      });
    },
    onSuccess: async () => {
      setName("");
      setChatId("");
      await queryClient.invalidateQueries({ queryKey: CHANNELS_KEY });
      onDone();
    },
    onError: (e) => setError(errorText(e)),
    onSettled: () => setToken(""),
  });

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (save.isPending) return;
    if (!name.trim() || !chatId.trim() || (!channel && !token)) {
      setError(channel ? "Name and chat ID are required." : "All fields are required.");
      return;
    }
    setError(null);
    save.mutate();
  }

  return (
    <form
      onSubmit={onSubmit}
      noValidate
      aria-label={channel ? `Edit ${channel.name}` : "Add a Telegram channel"}
      className="grid gap-4 md:grid-cols-3"
    >
      <div>
        <label htmlFor={`${prefix}-name`} className={label}>
          Name
        </label>
        <input
          id={`${prefix}-name`}
          maxLength={128}
          className={input}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </div>
      <div>
        <label htmlFor={`${prefix}-chat`} className={label}>
          Chat ID
        </label>
        <input
          id={`${prefix}-chat`}
          autoComplete="off"
          spellCheck={false}
          maxLength={33}
          className={input}
          value={chatId}
          onChange={(e) => setChatId(e.target.value)}
          aria-describedby={`${prefix}-chat-hint`}
        />
        <p id={`${prefix}-chat-hint`} className={fieldHint}>
          A numeric chat ID (groups start with -) or @channelname.
        </p>
      </div>
      <div>
        <label htmlFor={`${prefix}-token`} className={label}>
          Bot token
        </label>
        <input
          id={`${prefix}-token`}
          type="password"
          autoComplete="new-password"
          spellCheck={false}
          maxLength={160}
          className={input}
          value={token}
          onChange={(e) => setToken(e.target.value)}
          aria-describedby={`${prefix}-token-hint`}
        />
        <p id={`${prefix}-token-hint`} className={fieldHint}>
          {channel
            ? `${tokenSet ? "Set" : "Not set"}. Leave empty to keep the stored token.`
            : "From @BotFather. Stored encrypted and never shown again."}
        </p>
      </div>
      <div className="flex flex-col gap-3 md:col-span-3">
        {error && (
          <p role="alert" className={alertError}>
            {error}
          </p>
        )}
        <div className="flex flex-wrap gap-2">
          <button type="submit" disabled={save.isPending} className={buttonPrimary}>
            {save.isPending ? "Saving…" : channel ? "Save" : "Add channel"}
          </button>
          {channel && (
            <button type="button" onClick={onDone} className={buttonSecondary}>
              Cancel
            </button>
          )}
        </div>
      </div>
    </form>
  );
}

function ChannelRow({ channel }: { channel: Channel }) {
  const api = useApiClient();
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<"idle" | "edit" | "confirm-delete">("idle");
  const [result, setResult] = useState<ChannelTestResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const test = useMutation({
    mutationFn: () => api.testChannel(channel.id),
    onSuccess: setResult,
    onError: (e) => setError(errorText(e)),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteChannel(channel.id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: CHANNELS_KEY }),
    onError: (e) => {
      setMode("idle");
      setError(errorText(e));
    },
  });

  function start(next: typeof mode) {
    setError(null);
    setResult(null);
    setMode(next);
  }

  return (
    <li className={card}>
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div className="flex min-w-0 flex-col gap-1">
          <span className="font-medium break-all">{channel.name}</span>
          <span className="text-xs break-all text-slate-600 dark:text-slate-400">
            Telegram · chat {channel.config.chat_id ?? "—"} · bot token{" "}
            {channel.secrets_set.bot_token ? "set" : "not set"}
          </span>
        </div>
        <div className="flex flex-wrap gap-2 md:justify-end">
          {mode === "confirm-delete" ? (
            <>
              <button
                type="button"
                disabled={remove.isPending}
                onClick={() => remove.mutate()}
                className={buttonDanger}
                aria-label={`Confirm deleting ${channel.name}`}
              >
                Confirm delete
              </button>
              <button type="button" onClick={() => start("idle")} className={buttonSecondary}>
                Keep
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                disabled={test.isPending}
                onClick={() => {
                  start("idle");
                  test.mutate();
                }}
                className={buttonSecondary}
                aria-label={`Send a test message to ${channel.name}`}
              >
                {test.isPending ? "Sending…" : "Send test message"}
              </button>
              <button
                type="button"
                onClick={() => start(mode === "edit" ? "idle" : "edit")}
                className={buttonSecondary}
                aria-label={`Edit ${channel.name}`}
                aria-expanded={mode === "edit"}
              >
                Edit
              </button>
              <button
                type="button"
                onClick={() => start("confirm-delete")}
                className={buttonSecondary}
                aria-label={`Delete ${channel.name}`}
              >
                Delete
              </button>
            </>
          )}
        </div>
      </div>
      {mode === "edit" && (
        <div className="mt-3 border-t border-slate-200 pt-3 dark:border-slate-800">
          <ChannelForm channel={channel} onDone={() => setMode("idle")} />
        </div>
      )}
      {result && (
        <p role="status" className={`${result.ok ? alertInfo : alertError} mt-3`}>
          {result.ok ? "Test message sent." : `Test failed: ${result.message}`}
        </p>
      )}
      {error && (
        <p role="alert" className={`${alertError} mt-3`}>
          {error}
        </p>
      )}
    </li>
  );
}

function ChannelsSection() {
  const api = useApiClient();
  const channels = useQuery({ queryKey: CHANNELS_KEY, queryFn: () => api.channels() });

  let list;
  if (channels.isPending) {
    list = (
      <p role="status" className="text-sm text-slate-600 dark:text-slate-400">
        Loading channels…
      </p>
    );
  } else if (channels.isError) {
    list = (
      <div role="alert" className={`${alertError} flex items-center justify-between gap-3`}>
        <span>Could not load the channels: {errorText(channels.error)}</span>
        <button type="button" className={buttonSecondary} onClick={() => void channels.refetch()}>
          Try again
        </button>
      </div>
    );
  } else if (channels.data.length === 0) {
    list = <EmptyState>No channels yet. Add a Telegram bot below.</EmptyState>;
  } else {
    list = (
      <ul aria-label="Channels" className="space-y-3">
        {channels.data.map((channel) => (
          <ChannelRow key={channel.id} channel={channel} />
        ))}
      </ul>
    );
  }

  return (
    <>
      <section aria-labelledby="channels-title" className="space-y-4">
        <h2 id="channels-title" className="text-base font-semibold">
          Channels
        </h2>
        <p className="text-sm text-slate-600 dark:text-slate-400">
          Where alerts are sent. Messages go from the Vantage server to api.telegram.org; the bot
          token stays on the server.
        </p>
        {list}
        <div className={card}>
          <h3 className="mb-3 text-sm font-semibold">Add a Telegram channel</h3>
          <ChannelForm onDone={() => undefined} />
        </div>
      </section>
    </>
  );
}

export function AlertsPage() {
  const isAdmin = useUser().role === "admin";
  return (
    <div className="space-y-8">
      <PageHeader title="Alerts" />
      <EventsSection />
      {isAdmin && (
        <>
          <RulesSection />
          <ChannelsSection />
        </>
      )}
    </div>
  );
}
