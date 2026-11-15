Saved page: https://pebblecount.example/docs/cli

# The pebble command line

Welcome to the terminal side of the jar! Everything you can do in the app you can do with `pebble`. All data lives in one file, your jar, which by default is `jar.json` in the current folder. Point at a different jar with `--jar PATH` on any command.

Every command takes an optional `--today YYYY-MM-DD` so you can say what "today" is. If you leave it out, pebble uses your computer's date.

## pebble add NAME

Starts a new habit and prints its id. Ids are short and count up from 1 (`h1`, `h2`, ...). Habit names must be unique, ignoring upper and lower case, and can be up to 60 characters.

Trying to add a name that already exists fails with exit code 2 and the message:
`That pebble is already in the jar! Pick a different name.`

## pebble check ID [--date YYYY-MM-DD]

Drops a pebble for a habit: records a check-in for the given date (default: today). You can back-fill earlier dates, but you can't check in for a date after today, and you can't check in for a date before the habit was created.

A second check-in for the same habit on the same date fails with exit code 2 and the message:
`Whoa there! You already dropped a pebble today.`

An unknown habit id fails with exit code 3: `Hmm, we can't find that habit in your jar.`

## pebble streak ID

Prints the habit's current streak as a whole number of days.

How we count: walk backwards one day at a time, starting from today. Each day with a pebble adds one to the streak. If today has no pebble yet, we start from yesterday instead, so your streak doesn't vanish at midnight. A day without a pebble ends the streak, unless your Skip Stone covers it. The Skip Stone forgives a missed day when there is no other forgiven day in the 6 days before it (that is, one forgiven miss in any rolling 7 days). A forgiven day keeps the streak alive but does not add to the count. The streak never reaches back before the day the habit was created. Two missed days in a row always end the streak.

## pebble list

Prints one line per habit: id, name, current streak, and total pebbles, in the order the habits were added.

## pebble remove ID

Removes a habit and all its pebbles. Ids are never reused.

## pebble export [--out PATH]

Writes your whole jar as an export file (see the export format page). Without `--out`, prints it to standard output.

## pebble import PATH

Loads an export file into the jar. Habits whose names already exist in the jar are skipped, not merged. Imported habits get fresh ids.

## Exit codes

0 all good, 2 your input was not valid, 3 not found, 4 the jar file is damaged.
