// Client-side checks that mirror the backend's (app/api/auth.py). The backend checks
// again; these only give a quicker, clearer message.

export const MIN_PASSWORD_LENGTH = 12;
export const MAX_PASSWORD_LENGTH = 1024;
export const USERNAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;

export const USERNAME_HINT =
  "1 to 64 characters: letters, digits, '.', '_' or '-', starting with a letter or digit.";
export const PASSWORD_HINT = `${MIN_PASSWORD_LENGTH} to ${MAX_PASSWORD_LENGTH} characters.`;

/** Why a new password (typed twice) can't be used, or null if it can. */
export function newPasswordProblem(password: string, repeated: string): string | null {
  if (password.length < MIN_PASSWORD_LENGTH || password.length > MAX_PASSWORD_LENGTH) {
    return `The password must be ${MIN_PASSWORD_LENGTH} to ${MAX_PASSWORD_LENGTH} characters long.`;
  }
  if (password !== repeated) return "The passwords don't match.";
  return null;
}
