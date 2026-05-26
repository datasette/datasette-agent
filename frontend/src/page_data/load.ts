/**
 * Load typed page data from the embedded JSON script tag.
 * The server renders page data as JSON inside <script id="pageData">.
 */
export function loadPageData<T>(): T {
  const script = document.querySelector<HTMLScriptElement>("#pageData");
  if (!script?.textContent) {
    throw new Error("No #pageData script tag found");
  }
  return JSON.parse(script.textContent) as T;
}
