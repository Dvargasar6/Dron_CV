I am working on a computer vision-guided drone project. The main objective is for the drone to follow a path of number-shaped mylar balloons in order (1, 2, 3...).
The drone's controller is an ESP32-CAM.

I need to train a computer vision model to recognize Mylar balloons and the number they contain.

##########################

The approach we will follow is as follows:

- The MCU and the PC will communicate with a local server via WiFi. The MCU will send the image to the PC, which will execute the code to perform the recognition.

- A first YOLO model will recognize Mylar balloons as objects. The code will crop the region of interest (ROI) of the identified balloon, and this image will be analyzed by a second classification model (YOLO-cls) that will determine which number the balloon contain. 

- The drone must recognize the first balloon (labeled with the number 1) and collide with it; then, recognize the second balloon (number 2) and collide with it, and so on until the circuit is completed. The last number to be recognized can be left as a variable.

- The entire computer vision system must be easily integrable into the drone system. That is being developed by others.
 
#########################

Your instructions are:

- Generate a README.md file you must structure a more detailed action plan, defining the empty decisions. In this file must be the description of project, the project status, all commands and instructions for execute and the explanation of all codes in the project. This file must be update constantly. The sections in this file must be concealable. 

- Generate a file requirement.txt for create a Conda environment called drone_env with the necessary dependencies.

- Generate the necessary code following the action plan, verifying the success of the current step before moving on to the next. Do not execute anything; simply write the code and provide me with instructions on how to run it.


#########################

The action plan we will follow is:

- Generate the README.md with the detailed action plan and the requeriments.txt for the environment.

- Achieve communication between the PC and the MCU via WiFi. We must see the image of camera since PC. For this we need a GUI for visualize.

- Load a pre-trained YOLO model onto the MCU to recognize common objects. The specific YOLO model has not yet been selected. 

- Train a YOLO model to recognize Mylar balloons. The training must be performed on Google Colab, as my PC lacks an Nvidia GPU to carry it out.

- Train a model or use a pre-trained model to classify the numbers—or their absence—in input images.

- Generate the code for complete recognition, detect balloon and classify number. 


##########################

Some pending decisions, you must help me to choose:

- Communication protocols between the MCU and PC. Some ideas can be HTTP MJPEG, RTSP, Sockets UDP. 

- Models.

