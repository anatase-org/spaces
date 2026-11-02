/* SPDX-License-Identifier: AGPL-3.0-or-later */
/* Zero-context unified hunks over the rendered tree. NULL requests a full read.
 * Tree rows are newline-terminated; labels and values have JSON escaping. */
#include <glib.h>
#include <string.h>

static void tree_diff_range(GString *out, gsize start, gsize count)
{
    g_string_append_printf(out, "%" G_GSIZE_FORMAT, start + (count != 0));
    if (count != 1) g_string_append_printf(out, ",%" G_GSIZE_FORMAT, count);
}

static char *tree_diff(const char *previous, const char *current, const char *unchanged_text)
{
    if (!strcmp(previous, current)) return g_strdup(unchanged_text);
    char **old = g_strsplit(previous, "\n", -1), **now = g_strsplit(current, "\n", -1);
    gsize old_count = g_strv_length(old), new_count = g_strv_length(now);
    if (old_count && !*old[old_count - 1]) old_count--;
    if (new_count && !*now[new_count - 1]) new_count--;
    gsize prefix = 0, suffix = 0;
    while (prefix < old_count && prefix < new_count && !strcmp(old[prefix], now[prefix])) prefix++;
    while (suffix < old_count - prefix && suffix < new_count - prefix &&
        !strcmp(old[old_count - suffix - 1], now[new_count - suffix - 1])) suffix++;
    gsize n = old_count - prefix - suffix, m = new_count - prefix - suffix;
    char *result = NULL;
    if (n && m > 6000000 / n) goto done;
    gsize width = m + 1;
    guint32 *lcs = g_try_new0(guint32, (n + 1) * width);
    if (!lcs) goto done;
    for (gsize i = n; i-- > 0;) {
        for (gsize j = m; j-- > 0;) {
            lcs[i * width + j] = !strcmp(old[prefix + i], now[prefix + j])
                ? 1 + lcs[(i + 1) * width + j + 1]
                : MAX(lcs[(i + 1) * width + j], lcs[i * width + j + 1]);
        }
    }
    GString *out = g_string_new(NULL);
    gsize i = 0, j = 0;
    while (i < n || j < m) {
        if (i < n && j < m && !strcmp(old[prefix + i], now[prefix + j])) {
            i++; j++;
            continue;
        }
        gsize start_i = i, start_j = j;
        GString *removed = g_string_new(NULL), *added = g_string_new(NULL);
        while (i < n || j < m) {
            if (i < n && j < m && !strcmp(old[prefix + i], now[prefix + j])) break;
            if (i < n && (j == m || lcs[(i + 1) * width + j] >= lcs[i * width + j + 1])) {
                g_string_append_printf(removed, "-%s\n", old[prefix + i++]);
            } else {
                g_string_append_printf(added, "+%s\n", now[prefix + j++]);
            }
        }
        g_string_append(out, "@@ -");
        tree_diff_range(out, prefix + start_i, i - start_i);
        g_string_append(out, " +");
        tree_diff_range(out, prefix + start_j, j - start_j);
        g_string_append_printf(out, " @@\n%s%s", removed->str, added->str);
        g_string_free(removed, TRUE);
        g_string_free(added, TRUE);
    }
    g_free(lcs);
    /* Measure the complete candidate, including hunk headers and prefixes, in
     * Unicode characters rather than bytes or the share of rows changed. */
    if (5 * g_utf8_strlen(out->str, -1) <= 3 * g_utf8_strlen(current, -1))
        result = g_string_free(out, FALSE);
    else
        g_string_free(out, TRUE);
done:
    g_strfreev(old);
    g_strfreev(now);
    return result;
}
