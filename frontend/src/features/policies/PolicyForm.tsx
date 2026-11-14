import { useState, type FormEvent } from "react";
import { ApiError } from "../../api/client";
import { INSURANCE_TYPES, type InsuranceType, type Policy, type PolicyCreate } from "../../api/types";
import { FormField } from "../../components/FormField";
import { INSURANCE_LABELS } from "../../components/format";
import { Notice } from "../../components/States";
import { toApiError } from "../../components/useAsync";
import styles from "./Policies.module.css";

export interface PolicyFormValues {
  policy_number: string;
  name: string;
  insurance_type: InsuranceType | "";
}

// Usability checks only; the server's rules (database constraints) stay authoritative and their errors are shown.
export function validatePolicy(values: PolicyFormValues): Record<string, string> {
  const errors: Record<string, string> = {};
  const number = values.policy_number.trim();
  if (!number) errors.policy_number = "Enter the policy number.";
  else if (number.length > 100) errors.policy_number = "Use at most 100 characters.";
  if (!values.name.trim()) errors.name = "Enter the policy name.";
  else if (values.name.trim().length > 300) errors.name = "Use at most 300 characters.";
  if (!values.insurance_type) errors.insurance_type = "Choose the insurance type.";
  return errors;
}

export function PolicyForm({ initial, submitLabel, busyLabel, onSubmit, onCancel }: {
  initial?: Policy;
  submitLabel: string;
  busyLabel: string;
  onSubmit: (values: PolicyCreate) => Promise<void>;
  onCancel: () => void;
}) {
  const [values, setValues] = useState<PolicyFormValues>({
    policy_number: initial?.policy_number ?? "",
    name: initial?.name ?? "",
    insurance_type: initial?.insurance_type ?? "",
  });
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [failure, setFailure] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const set = (field: keyof PolicyFormValues) => (value: string) => setValues((v) => ({ ...v, [field]: value }));

  async function submit(event: FormEvent) {
    event.preventDefault();
    const found = validatePolicy(values);
    setErrors(found);
    setFailure(null);
    if (Object.keys(found).length > 0) return;
    setBusy(true);
    try {
      await onSubmit({
        policy_number: values.policy_number.trim(),
        name: values.name.trim(),
        insurance_type: values.insurance_type as InsuranceType,
      });
    } catch (caught) {
      const error = toApiError(caught);
      if (error.kind === "conflict" && error.message.includes("policy with this number")) {
        setErrors({ policy_number: error.message });
      } else if (error instanceof ApiError && Object.keys(error.fieldErrors).length > 0) {
        setErrors(error.fieldErrors);
        setFailure(error.message);
      } else {
        setFailure(error.message);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className={styles.form} onSubmit={submit} noValidate aria-busy={busy}>
      {failure ? <Notice tone="error">{failure}</Notice> : null}
      <FormField label="Policy number" hint="As printed on the policy schedule." error={errors.policy_number}>
        <input className="input" value={values.policy_number} maxLength={100} disabled={busy}
          onChange={(event) => set("policy_number")(event.target.value)} />
      </FormField>
      <FormField label="Policy name" error={errors.name}>
        <input className="input" value={values.name} maxLength={300} disabled={busy}
          onChange={(event) => set("name")(event.target.value)} />
      </FormField>
      <FormField label="Insurance type" error={errors.insurance_type}>
        <select className="select" value={values.insurance_type} disabled={busy}
          onChange={(event) => set("insurance_type")(event.target.value)}>
          <option value="">Choose a type</option>
          {INSURANCE_TYPES.map((type) => (
            <option key={type} value={type}>
              {INSURANCE_LABELS[type]}
            </option>
          ))}
        </select>
      </FormField>
      <div className={styles.formActions}>
        <button type="submit" className="button button-primary" disabled={busy}>
          {busy ? busyLabel : submitLabel}
        </button>
        <button type="button" className="button button-secondary" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </form>
  );
}
