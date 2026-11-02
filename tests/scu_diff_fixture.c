/* SPDX-License-Identifier: AGPL-3.0-or-later */
#include "../native/scu_diff.h"

char *scu_test_diff(const char *previous, const char *current)
{
    return tree_diff(previous, current);
}

void scu_test_free(char *text)
{
    g_free(text);
}
