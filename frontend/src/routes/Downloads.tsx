// Downloads view (owner): submit a gallery URL, check the GP balance/cost, and
// watch the job list with live polling and per-job priority controls.

import { useCallback, useState } from 'react';
import {
  getBalance,
  listJobs,
  setJobPriority,
  submitDownload,
} from '../api/endpoints';
import type { BalanceResponse, DownloadJob } from '../api/types';
import { usePolling } from '../hooks/useApi';
import { Spinner, ErrorBanner } from '../components/ui';
import { t } from '../i18n/strings';

const POLL_MS = 3000;

export function Downloads() {
  const [url, setUrl] = useState('');
  const [catid, setCatid] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [submitMsg, setSubmitMsg] = useState<string | null>(null);
  const [balance, setBalance] = useState<BalanceResponse | null>(null);
  const [balanceErr, setBalanceErr] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  const jobsState = usePolling(
    (signal) => listJobs(signal),
    POLL_MS,
  );
  const jobs = jobsState.data?.jobs ?? [];

  const onCheckBalance = useCallback(async () => {
    if (!url.trim()) return;
    setChecking(true);
    setBalance(null);
    setBalanceErr(null);
    try {
      const res = await getBalance(url.trim());
      setBalance(res);
      if (res.error) setBalanceErr(res.error);
    } catch (err) {
      setBalanceErr(err instanceof Error ? err.message : String(err));
    } finally {
      setChecking(false);
    }
  }, [url]);

  const onSubmit = useCallback(async () => {
    if (!url.trim()) return;
    setSubmitting(true);
    setSubmitMsg(null);
    try {
      await submitDownload(url.trim(), catid.trim() || undefined);
      setSubmitMsg('Queued.');
      setUrl('');
      setBalance(null);
      jobsState.reload();
    } catch (err) {
      setSubmitMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }, [url, catid, jobsState]);

  const changePriority = useCallback(
    async (job: DownloadJob, delta: number) => {
      try {
        await setJobPriority(job.id, job.priority + delta);
        jobsState.reload();
      } catch {
        /* surfaced by next poll */
      }
    },
    [jobsState],
  );

  return (
    <div className="downloads">
      <h1>{t('downloads.title')}</h1>

      <section className="downloads__form panel">
        <div className="field-row">
          <input
            className="select downloads__url"
            placeholder={t('downloads.url')}
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
          <input
            className="select downloads__cat"
            placeholder="catid (optional)"
            value={catid}
            onChange={(e) => setCatid(e.target.value)}
          />
        </div>
        <div className="downloads__buttons">
          <button
            type="button"
            className="btn"
            onClick={onCheckBalance}
            disabled={checking || !url.trim()}
          >
            {checking ? '…' : t('downloads.checkBalance')}
          </button>
          <button
            type="button"
            className="btn btn--primary"
            onClick={onSubmit}
            disabled={submitting || !url.trim()}
          >
            {t('downloads.submit')}
          </button>
        </div>

        {balanceErr && <div className="error-banner">{balanceErr}</div>}
        {balance && !balanceErr && (
          <div className="balance">
            {balance.gallery_title && (
              <div className="balance__title">{balance.gallery_title}</div>
            )}
            <div className="balance__grid">
              <span>{t('downloads.balance')}</span>
              <strong>{balance.balance ?? '—'} GP</strong>
              <span>{t('downloads.cost')} (Original)</span>
              <strong>{balance.original_cost ?? '—'} GP</strong>
              <span>{t('downloads.cost')} (Resample)</span>
              <strong>{balance.resample_cost ?? 'free'}</strong>
            </div>
            {balance.sufficient === false && (
              <div className="balance__warn">⚠ Insufficient GP for Original</div>
            )}
          </div>
        )}
        {submitMsg && <div className="downloads__msg">{submitMsg}</div>}
      </section>

      <section className="downloads__jobs">
        <h2>{t('downloads.jobs')}</h2>
        {jobsState.error && <ErrorBanner error={jobsState.error} onRetry={jobsState.reload} />}
        {jobsState.loading && jobs.length === 0 && <Spinner />}
        {!jobsState.loading && jobs.length === 0 && (
          <p className="detail__muted">{t('downloads.noJobs')}</p>
        )}
        {jobs.length > 0 && (
          <table className="jobs-table">
            <thead>
              <tr>
                <th>{t('downloads.state')}</th>
                <th>URL / Title</th>
                <th>GP</th>
                <th>Progress</th>
                <th>{t('downloads.priority')}</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <JobRow key={job.id} job={job} onPriority={changePriority} />
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

function JobRow({
  job,
  onPriority,
}: {
  job: DownloadJob;
  onPriority: (job: DownloadJob, delta: number) => void;
}) {
  const pct =
    job.progress == null ? null : job.progress > 1 ? job.progress : job.progress * 100;
  return (
    <tr className={`job job--${job.state}`}>
      <td>
        <span className={`job-state job-state--${job.state}`}>{job.state}</span>
      </td>
      <td className="job__url" title={job.url}>
        {job.title || job.url}
        {job.error && <div className="job__error">{job.error}</div>}
      </td>
      <td>{job.gp_cost ?? '—'}</td>
      <td>
        {pct == null ? (
          '—'
        ) : (
          <div className="job__progress">
            <div className="job__progress-bar" style={{ width: `${Math.min(100, pct)}%` }} />
            <span>{Math.round(pct)}%</span>
          </div>
        )}
      </td>
      <td className="job__priority">
        <button
          type="button"
          className="btn btn--icon btn--small"
          onClick={() => onPriority(job, 1)}
          aria-label="Raise priority"
          disabled={job.state === 'done'}
        >
          ▲
        </button>
        <span>{job.priority}</span>
        <button
          type="button"
          className="btn btn--icon btn--small"
          onClick={() => onPriority(job, -1)}
          aria-label="Lower priority"
          disabled={job.state === 'done'}
        >
          ▼
        </button>
      </td>
    </tr>
  );
}
