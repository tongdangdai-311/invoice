# Description of the webpage of the provider.
I need to have a web app to scrapping the invoice of the system for my construction company.
The web app,  first things is to have scrapping function, where it can takes all of the information from the portal of provider, which trigger sign in.
Then I set up the auto scrapping using PlayWright, where the code can find the page that have the download button
When we click into that, it would have a spreadsheet of csv, like the sample in docs/sample

The Link to log-in page is in ./docs/url.txt





# Description of the csv file.
From the sample csv file, what we actually need from that is the the order_id, total_price and each of the item inside the file. on top of that, we do need to job name ( like harvard standish, 273  highands, etc), memo(short description of the tool for that job, like tools for plumping,...).


# ADR:

Hosting: Azure statis web service
Databasee: MySQL

language: Python, fatsAPi, playwright and numpy for data processing.
UI: simple html file.






