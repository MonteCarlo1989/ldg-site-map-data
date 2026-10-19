import { useQueryClient } from '@tanstack/react-query';
import {
  getGetAcquisitionStatusQueryKey,
  useGetAcquisitionStatus,
  useRunAcquisition,
} from '@workspace/api-client-react';

function fmt(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(2)} MB`;
}

function Raw({ id, title, text, empty }: { id: string; title: string; text: string; empty: string }) {
  return (
    <section aria-labelledby={id} className="mt-8">
      <h2 id={id} className="text-xs font-semibold uppercase tracking-widest text-muted-foreground mb-2">{title}</h2>
      <pre
        tabIndex={0}
        data-testid={`text-${id}`}
        className="font-mono text-xs leading-relaxed bg-card border rounded-sm p-3 overflow-auto max-h-[28rem] whitespace-pre-wrap break-words"
      >
        {text || empty}
      </pre>
    </section>
  );
}

export default function Status() {
  const qc = useQueryClient();
  const q = useGetAcquisitionStatus({
    query: {
      queryKey: getGetAcquisitionStatusQueryKey(),
      refetchInterval: (query) => (query.state.data?.running ? 2000 : 10000),
    },
  });
  const run = useRunAcquisition();
  const data = q.data;
  const running = !!data?.running || run.isPending;
  let manifestText = data?.manifest ?? '';
  let resultStatus = 'Idle.';
  try {
    const manifest = JSON.parse(manifestText);
    manifestText = JSON.stringify(manifest, null, 2);
    resultStatus = manifest.status === 'partial'
      ? `Finished with ${manifest.failed_datasets} failed datasets. See the manifest and log.`
      : manifest.status === 'complete' ? 'Acquisition complete.'
      : manifest.status === 'failed' ? 'Acquisition failed. See the manifest and log.'
      : 'Idle.';
  } catch { /* An acquisition may not yet have written its first manifest. */ }

  const start = () => {
    if (data?.files.length && !window.confirm('Rerunning replaces the files in data/. Download any files you need to keep before continuing.')) return;
    run.mutate(undefined, {
      onSuccess: (result) => qc.setQueryData(getGetAcquisitionStatusQueryKey(), result),
      onSettled: () => qc.invalidateQueries({ queryKey: getGetAcquisitionStatusQueryKey() }),
    });
  };

  return (
    <main className="min-h-[100dvh] px-4 py-8 sm:px-8">
      <div className="mx-auto max-w-3xl">
        <header className="border-b-2 border-primary pb-4 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-xl font-semibold">LDG Site Map Data</h1>
            <p className="text-sm text-muted-foreground">Perry, Taylor County, Florida. Acquisition record.</p>
          </div>
          <button
            type="button"
            onClick={start}
            disabled={running}
            data-testid="button-run"
            className="bg-primary text-primary-foreground px-4 py-2 text-sm font-medium rounded-sm disabled:opacity-60 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring"
          >
            {running ? 'Running...' : 'Run acquisition'}
          </button>
        </header>

        <p role="status" aria-live="polite" data-testid="status-state" className="mt-4 text-sm">
          {q.isLoading
            ? 'Loading status...'
            : running
              ? 'Acquisition is running. Status refreshes every 2 seconds.'
              : data && data.files.length === 0 && !data.manifest && !data.log
                ? 'No acquisition has been run yet.'
                : resultStatus}
        </p>

        {(q.isError || run.isError) && (
          <div role="alert" data-testid="status-error" className="mt-4 border border-destructive text-destructive rounded-sm p-3 text-sm">
            {run.isError ? 'Run request failed: ' : 'Status request failed: '}
            {((run.error ?? q.error) as Error | null)?.message ?? 'Unknown error'}
            {q.isError && (
              <button type="button" onClick={() => q.refetch()} data-testid="button-retry" className="ml-3 underline">
                Retry
              </button>
            )}
          </div>
        )}

        <section aria-labelledby="files-h" className="mt-8">
          <h2 id="files-h" className="text-xs font-semibold uppercase tracking-widest text-muted-foreground mb-2">
            Produced files ({data?.files.length ?? 0})
          </h2>
          {data && data.files.length > 0 ? (
            <ul className="border rounded-sm bg-card divide-y max-h-[24rem] overflow-auto">
              {data.files.map((f) => (
                <li key={f.name} className="flex justify-between gap-4 px-3 py-2 text-sm" data-testid={`row-file-${f.name}`}>
                  <a href={f.url} download className="font-mono text-primary underline break-all" data-testid={`link-file-${f.name}`}>
                    {f.name}
                  </a>
                  <span className="font-mono text-muted-foreground shrink-0">{fmt(f.size)}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground border border-dashed rounded-sm p-3">No files produced.</p>
          )}
        </section>

        <Raw id="manifest" title="Manifest" text={manifestText} empty="No manifest." />
        <Raw id="log" title="Request log" text={data?.log ?? ''} empty="No log entries." />
      </div>
    </main>
  );
}
