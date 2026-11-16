# Internship Alerts Bot 📬

An automated scraper that monitors company ATS boards (Greenhouse, Lever, Ashby, Workday), SimplifyJobs, and public LinkedIn postings, then sends email digests whenever new matching internships appear.

---

## How It Works

- **Scheduled Execution:** Runs automatically every 10 minutes via GitHub Actions.
- **Deduplication:** Keeps track of previously seen listings in `seen_jobs.db` and commits the updated database back to the repository.
- **First Run:** Indexes all current matching jobs (so you don't get flooded) and sends a single confirmation test email.
- **Subsequent Runs:** Only emails you when brand-new postings matching your filters are detected.

---

## Setup & Configuration

### 1. Gmail App Password
1. Navigate to [myaccount.google.com/security](https://myaccount.google.com/security).
2. Ensure **2-Step Verification** is enabled.
3. Search for **App passwords**, create an entry named `internship alerts`, and copy the 16-character code.

### 2. GitHub Secrets
In your GitHub repository, go to **Settings** > **Secrets and variables** > **Actions** and add three repository secrets:

| Secret Name | Value | Description |
| :--- | :--- | :--- |
| `SMTP_USER` | `tanveenk06@gmail.com` | Your Gmail address |
| `SMTP_PASS` | `xxxx xxxx xxxx xxxx` | 16-character App Password |
| `EMAIL_TO` | `tanveenk06@gmail.com` | Destination email for alerts |

### 3. GitHub Actions Permissions
Because the workflow commits `seen_jobs.db` back to the repository:
1. Go to **Settings** > **Actions** > **General**.
2. Scroll to **Workflow permissions**.
3. Select **Read and write permissions** and click **Save**.

### 4. Run Manually (First Time)
1. Go to the **Actions** tab in your repository.
2. Under **Workflows**, click **internship-alerts**.
3. Click **Run workflow** > **Run workflow**.
4. Check your inbox (and spam folder) for an email titled:  
   `"Internship alerts: set up and running"`

---

## Running Locally

To run locally on your own machine:
```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your SMTP credentials

# Run once
python internship_alerts.py

# Or run continuously
python internship_alerts.py --loop
```
