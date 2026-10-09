###generate_dashboard.py
import pymysql
import json
import webbrowser
import os
from dotenv import load_dotenv

# ==========================================
# CONFIGURATION
# ==========================================
# Point exactly to where your .env file lives
load_dotenv('/home/ps51632/mysite/explorecme/.env')

MYSQL_HOST = os.getenv('MYSQL_HOST')
MYSQL_USER = os.getenv('MYSQL_USER')
MYSQL_PASSWORD = os.getenv('MYSQL_PASSWORD')
MYSQL_DB = os.getenv('MYSQL_DB')

# The central table for all unique questions in the new schema
TABLE_NAME = 'question_bank'

def fetch_data():
    """Fetches the required statistics from the MySQL database."""
    try:
        conn = pymysql.connect(
            host=MYSQL_HOST,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DB
        )
    except pymysql.MySQLError as e:
        print(f"🚨 ERROR: Could not connect to MySQL database: {e}")
        return 0, [], []

    cursor = conn.cursor()

    # 1. Get Total Unique Questions
    cursor.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}")
    total_questions = cursor.fetchone()[0]

    # 2. Get Organ System Counts (Uppercase and trimmed)
    cursor.execute(f"""
        SELECT UPPER(TRIM(organ_system)), COUNT(*)
        FROM {TABLE_NAME}
        GROUP BY UPPER(TRIM(organ_system))
        ORDER BY COUNT(*) DESC
    """)
    organ_data = cursor.fetchall()

    # 3. Get Task Area Counts (Uppercase and trimmed)
    cursor.execute(f"""
        SELECT UPPER(TRIM(task_area)), COUNT(*)
        FROM {TABLE_NAME}
        GROUP BY UPPER(TRIM(task_area))
        ORDER BY COUNT(*) DESC
    """)
    task_data_raw = cursor.fetchall()

    conn.close()

    # Define mapping for truncating task area names
    task_area_mapping = {
        "HISTORY TAKING & PERFORMING PHYSICAL EXAMINATION": "History and PE",
        "HISTORY TAKING AND PERFORMING PHYSICAL EXAMINATION": "History and PE",
        "USING DIAGNOSTIC AND LABORATORY STUDIES": "Diagnostics",
        "USING LABORATORY AND DIAGNOSTIC STUDIES": "Diagnostics",
        "FORMULATING MOST LIKELY DIAGNOSIS": "Most Likely Diagnosis",
        "FORMULATING THE MOST LIKELY DIAGNOSIS": "Most Likely Diagnosis",
        "HEALTH MAINTENANCE, PATIENT EDUCATION, AND PREVENTATIVE MEASURES": "Patient Education",
        "HEALTH MAINTENANCE, PATIENT EDUCATION, AND PREVENTIVE MEASURES": "Patient Education",
        "CLINICAL INTERVENTION": "Clinical Intervention",
        "PHARMACEUTICAL THERAPEUTICS": "Therapeutics",
        "APPLYING FOUNDATIONAL SCIENTIFIC CONCEPTS": "Scientific Concepts",
        "APPLYING BASIC SCIENTIFIC CONCEPTS": "Scientific Concepts",
    }

    # Apply truncation to task_data
    task_data = []
    for area, count in task_data_raw:
        truncated_area = task_area_mapping.get(area, area)
        task_data.append((truncated_area, count))

    return total_questions, organ_data, task_data

def generate_html(total_questions, organ_data, task_data):
    """Generates an HTML file with Chart.js graphics."""

    # Prepare data for Chart.js
    organ_labels = [row[0] for row in organ_data]
    organ_counts = [row[1] for row in organ_data]

    task_labels = [row[0] for row in task_data]
    task_counts = [row[1] for row in task_data]

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Database Statistics Dashboard</title>
        <!-- Load Chart.js -->
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body {{
                font-family: Arial, sans-serif;
                background-color: #f4f7f6;
                color: #333;
                margin: 0;
                padding: 40px 20px;
            }}
            .container {{
                max-width: 1000px;
                margin: auto;
            }}
            .header-card {{
                background: linear-gradient(135deg, #007bff, #0056b3);
                color: white;
                padding: 30px;
                border-radius: 8px;
                text-align: center;
                box-shadow: 0 4px 6px rgba(0,0,0,0.1);
                margin-bottom: 30px;
            }}
            .header-card h1 {{ margin: 0; font-size: 2.5em; }}
            .header-card p {{ margin: 10px 0 0 0; font-size: 1.2em; opacity: 0.9; }}

            .grid {{
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 30px;
            }}
            .chart-card {{
                background: white;
                padding: 20px;
                border-radius: 8px;
                box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            }}
            .chart-card h2 {{
                margin-top: 0;
                color: #2c3e50;
                text-align: center;
                border-bottom: 2px solid #eee;
                padding-bottom: 10px;
            }}

            /* Responsive layout for smaller screens */
            @media (max-width: 768px) {{
                .grid {{ grid-template-columns: 1fr; }}
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <!-- Header section with Total Count -->
            <div class="header-card">
                <h1>{total_questions}</h1>
                <p>Total Unique Questions in Database</p>
            </div>

            <div class="grid">
                <!-- Organ Systems Chart -->
                <div class="chart-card">
                    <h2>Questions by Organ System</h2>
                    <canvas id="organChart"></canvas>
                </div>

                <!-- Task Areas Chart -->
                <div class="chart-card">
                    <h2>Questions by Task Area</h2>
                    <canvas id="taskChart"></canvas>
                </div>
            </div>
        </div>

        <script>
            // Data passed securely from Python to JS via JSON
            const organLabels = {json.dumps(organ_labels)};
            const organCounts = {json.dumps(organ_counts)};

            const taskLabels = {json.dumps(task_labels)};
            const taskCounts = {json.dumps(task_counts)};

            // Shared background colors for pretty charts
            const bgColors = [
                'rgba(54, 162, 235, 0.7)',
                'rgba(255, 99, 132, 0.7)',
                'rgba(255, 206, 86, 0.7)',
                'rgba(75, 192, 192, 0.7)',
                'rgba(153, 102, 255, 0.7)',
                'rgba(255, 159, 64, 0.7)'
            ];
            const borderColors = bgColors.map(color => color.replace('0.7', '1'));

            // 1. Render Organ System Pie Chart
            const ctxOrgan = document.getElementById('organChart').getContext('2d');
            new Chart(ctxOrgan, {{
                type: 'doughnut',
                data: {{
                    labels: organLabels,
                    datasets: [{{
                        label: 'Questions',
                        data: organCounts,
                        backgroundColor: bgColors,
                        borderColor: borderColors,
                        borderWidth: 1
                    }}]
                }},
                options: {{
                    responsive: true,
                    plugins: {{
                        legend: {{ position: 'bottom' }}
                    }}
                }}
            }});

            // 2. Render Task Area Bar Chart
            const ctxTask = document.getElementById('taskChart').getContext('2d');
            new Chart(ctxTask, {{
                type: 'bar',
                data: {{
                    labels: taskLabels,
                    datasets: [{{
                        label: 'Questions',
                        data: taskCounts,
                        backgroundColor: 'rgba(75, 192, 192, 0.7)',
                        borderColor: 'rgba(75, 192, 192, 1)',
                        borderWidth: 1
                    }}]
                }},
                options: {{
                    indexAxis: 'y', // Makes the bar chart horizontal for long text labels
                    responsive: true,
                    plugins: {{
                        legend: {{ display: false }}
                    }},
                    scales: {{
                        x: {{ beginAtZero: true }},
                        y: {{ // Adjust y-axis to allow for more space for labels
                            ticks: {{
                                autoSkip: false, // Ensure all labels are shown
                                font: {{
                                    size: 10 // Adjust font size if labels are too long
                                }}
                            }}
                        }}
                    }}
                }}
            }});
        </script>
    </body>
    </html>
    """

    # Save to a file
    output_filename = "/home/ps51632/mysite/db_dashboard.html"
    with open(output_filename, "w", encoding="utf-8") as f:
        f.write(html_content)

    return output_filename

if __name__ == "__main__":
    print("Fetching database statistics from MySQL...")
    total_q, organs, tasks = fetch_data()

    if total_q == 0 and not organs and not tasks:
        print("No data found or connection failed. Exiting.")
    else:
        print("Generating HTML dashboard...")
        html_file = generate_html(total_q, organs, tasks)

        print(f"Done! Opening {html_file} in your default web browser.")
        webbrowser.open('file://' + os.path.realpath(html_file))