import pybullet as p
import numpy as np
from wheeledSim.randomTerrain import *
from wheeledSim.randomExplorationPolicy import *

class simController:
    # this class controls the simulation. It controls the terrain and robot, and returns data
    def __init__(self,robot,physicsClientId=0,simulationParamsIn={},senseParamsIn={},
                terrainMapParamsIn={},terrainParamsIn={},explorationParamsIn={}):
        # set up simulation params
        self.simulationParams = {"timeStep":1./500.,
                            "stepsPerControlLoop":50,
                            "numSolverIterations":300,
                            "gravity":-10,
                            "contactBreakingThreshold":0.0001,
                            "contactSlop":0.0001,
                            "moveThreshold":0,
                            "maxStopMoveLength":np.inf,
                            "terminateIfFlipped":False,
                            "randomActionScale":[1,1]}
        self.simulationParams.update(simulationParamsIn)
        
        # set up robot sensing parameters
        self.senseParams = {"senseDim":[5,5], # width (meter or angle) and height (meter or angle) of terrain map or point cloud
                            "lidarAngleOffset":[0,0],
                            "lidarRange":10,
                            "senseResolution":[100,100], # array giving resolution of map output (num pixels wide x num pixels high)
                            "removeInvalidPointsInPC":False, # remove invalid points in point cloud
                            "senseType":-1, # 0 for terrainMap, 1 for lidar depth image, 2 for lidar point cloud, -1 for nothing
                            "sensorPose":[[0,0,0],[0,0,0,1]], # pose of sensor relative to body
                            "recordJointStates":False} # whether to record joint data or not
        self.senseParams.update(senseParamsIn)

        # set up simulation
        self.physicsClientId=physicsClientId
        self.timeStep = self.simulationParams["timeStep"]
        # Each control loop makes this many simulation steps. The period of a control loop is timeStep*stepsPerControlLoop
        self.stepsPerControlLoop=self.simulationParams["stepsPerControlLoop"]
        p.setPhysicsEngineParameter(numSolverIterations=self.simulationParams["numSolverIterations"],
            contactBreakingThreshold=self.simulationParams["contactBreakingThreshold"],contactSlop=self.simulationParams["contactSlop"],
            physicsClientId=self.physicsClientId)
        p.setGravity(0,0,self.simulationParams["gravity"],physicsClientId=self.physicsClientId)
        p.setTimeStep(self.timeStep,physicsClientId=self.physicsClientId)

        # set up terrain
        self.terrainParamsIn = {"terrainType": "randomRockyTerrain",
                            "existingTerrain": None}
        self.terrainParamsIn.update(terrainParamsIn)
        if self.terrainParamsIn["existingTerrain"]!=None:
            self.terrain = self.terrainParamsIn["existingTerrain"]
        else:
            if self.terrainParamsIn["terrainType"] == "randomRockyTerrain":
                self.terrain = randomRockyTerrain(terrainMapParamsIn,physicsClientId=self.physicsClientId)
            elif self.terrainParamsIn["terrainType"] == "randomSloped":
                self.terrain = randomSloped(terrainMapParamsIn,physicsClientId=self.physicsClientId)
            else:
                self.terrain = self.terrainParamsIn["terrainType"](terrainMapParamsIn,physicsClientId=self.physicsClientId)
            self.newTerrain()

        # set up determination of wheter robot is stuck
        self.moveThreshold = self.simulationParams["moveThreshold"]*self.simulationParams["moveThreshold"] # store square distance for easier computation later
        self.lastX = 0
        self.lastY = 0
        self.stopMoveCount =0

        # set up random driving
        explorationParams = {"explorationType":"boundedExplorationNoise"}
        explorationParams.update(explorationParamsIn)
        if explorationParams["explorationType"] == "boundedExplorationNoise":
            self.randDrive = boundedExplorationNoise(explorationParams)
        elif explorationParams["explorationType"] == "fixedRandomAction":
            self.randDrive = fixedRandomAction(explorationParams)
        #self.randDrive = ouNoise()
        #self.randDrive = np.zeros(2)

        # set up robot
        self.camFollowBot = False
        self.robot = robot
        self.lastStateRecordFlag = False # Flag to tell if last state of robot has been recorded or not
        self.resetRobot()

    # generate new terrain
    def newTerrain(self,**kwargs):
        self.terrain.generate(self.terrainParamsIn,**kwargs)

    # reset the robot
    def resetRobot(self,doFall=True,pos=[0,0],orien=[0,0,0,1]):
        self.controlLoopStep([0,0])
        if len(pos)>2:
            safeFallHeight = pos[2]
        else:
            safeFallHeight = self.terrain.maxLocalHeight(pos,1)+0.3
        self.robot.reset([[pos[0],pos[1],safeFallHeight],orien])
        if doFall:
            fallTime=0.5
            fallSteps = int(np.ceil(fallTime/self.timeStep))
            for i in range(fallSteps):
                self.stepSim()
        self.stopMoveCount = 0
        self.randDrive.reset()

    def stepSim(self):
        self.robot.updateSpringForce()
        p.stepSimulation(physicsClientId=self.physicsClientId)
        self.lastStateRecordFlag = False
        if self.camFollowBot:
            pose = self.robot.getPositionOrientation()
            pos = pose[0]
            orien = pose[1]
            forwardDir = p.multiplyTransforms([0,0,0],orien,[1,0,0],[0,0,0,1])[0]
            headingAngle = np.arctan2(forwardDir[1],forwardDir[0])*180/np.pi-90
            p.resetDebugVisualizerCamera(1.0,headingAngle,-15,pos,physicsClientId=self.physicsClientId)

    def controlLoopStep(self,driveCommand):
        throttle = driveCommand[0]
        steering = driveCommand[1]
        # Create Prediction Input
        # check if last pose of robot has been recorded
        if not self.lastStateRecordFlag:
            self.lastPose = self.robot.getPositionOrientation()
            self.lastVel = self.robot.getBaseVelocity_body()
            if self.senseParams["recordJointStates"]:
                self.lastJointState = self.robot.measureJoints()
                self.lastAbsoluteState = list(self.lastPose[0])+list(self.lastPose[1])+self.lastVel[:] + self.lastJointState[:]
            else:
                self.lastAbsoluteState = list(self.lastPose[0])+list(self.lastPose[1])+self.lastVel[:]
        #simulate sensing (generate height map or lidar point cloud)
        sensingData = self.sensing(self.lastPose)
        # store state-action for motion prediction
        stateActionData = [self.lastAbsoluteState,sensingData,driveCommand] #(absolute robot state, sensing data, action)
        # command robot throttle & steering and simulate
        self.robot.drive(throttle)
        self.robot.steer(steering)
        for i in range(self.stepsPerControlLoop):
            self.stepSim()
        # Record outcome state
        newPose = self.robot.getPositionOrientation()
        # check how long robot has been stuck
        if (newPose[0][0]-self.lastX)*(newPose[0][0]-self.lastX) + (newPose[0][1]-self.lastY)*(newPose[0][1]-self.lastY)> self.moveThreshold:
            self.lastX = newPose[0][0]
            self.lastY = newPose[0][1]
            self.stopMoveCount = 0
        else:
            self.stopMoveCount +=1
        # relative position, body twist, joint position and velocity
        self.lastPose = newPose
        self.lastVel = self.robot.getBaseVelocity_body()
        if self.senseParams["recordJointStates"]:
            self.lastJointState = self.robot.measureJoints()
            self.lastAbsoluteState = list(self.lastPose[0])+list(self.lastPose[1])+self.lastVel[:] + self.lastJointState[:]
        else:
            self.lastAbsoluteState = list(self.lastPose[0])+list(self.lastPose[1])+self.lastVel[:]
        self.lastStateRecordFlag = True
        newStateData = [self.lastAbsoluteState]
        return stateActionData,newStateData,self.simTerminateCheck(newPose)

    # check if simulation should be terminated
    def simTerminateCheck(self,robotPose):
        termSim = False
        # flipped robot termination criteria
        if self.simulationParams["terminateIfFlipped"]:
            upDir = p.multiplyTransforms([0,0,0],robotPose[1],[0,0,1],[0,0,0,1])[0]
            if upDir[2] < 0:
                termSim = True
        # stuck robot terminate criteria
        if self.stopMoveCount > self.simulationParams["maxStopMoveLength"]:
            termSim = True
        # boundary criteria
        minZ = np.min(self.terrain.gridZ) - 1.
        maxZ = np.max(self.terrain.gridZ) + 1.
        minX = np.min(self.terrain.gridX) + 1.
        maxX = np.max(self.terrain.gridX) - 1.
        minY = np.min(self.terrain.gridY) + 1.
        maxY = np.max(self.terrain.gridY) - 1.
        if robotPose[0][0] > maxX or robotPose[0][0] < minX or \
        robotPose[0][1] > maxY or robotPose[0][1] < minY or \
        robotPose[0][2] > maxZ or robotPose[0][2] < minZ:
            termSim = True
        return termSim

    # generate sensing data
    def sensing(self,robotPose,senseType=None,expandDim=False):
        if senseType is None:
            senseType = self.senseParams["senseType"]
        if not isinstance(senseType,int):
            return [self.sensing(robotPose,senseType[i],expandDim) for i in range(len(senseType))]
        sensorAbsolutePose = p.multiplyTransforms(robotPose[0],robotPose[1],self.senseParams["sensorPose"][0],self.senseParams["sensorPose"][1])
        if senseType == -1: # no sensing
            sensorData = np.array([])
        elif senseType == 0: #get terrain height map
            sensorData = self.terrain.sensedHeightMap(sensorAbsolutePose,self.senseParams["senseDim"],self.senseParams["senseResolution"])
        else: # get lidar data
            horzAngles = np.linspace(-self.senseParams["senseDim"][0]/2.,self.senseParams["senseDim"][0]/2.,self.senseParams["senseResolution"][0]+1)+self.senseParams["lidarAngleOffset"][0]
            horzAngles = horzAngles[0:-1]
            vertAngles = np.linspace(-self.senseParams["senseDim"][1]/2.,self.senseParams["senseDim"][1]/2.,self.senseParams["senseResolution"][1])+self.senseParams["lidarAngleOffset"][1]
            horzAngles,vertAngles = np.meshgrid(horzAngles,vertAngles)
            originalShape = horzAngles.shape
            horzAngles = horzAngles.reshape(-1)
            vertAngles = vertAngles.reshape(-1)
            sensorRayX = np.cos(horzAngles)*np.cos(vertAngles)*self.senseParams["lidarRange"]
            sensorRayY = np.sin(horzAngles)*np.cos(vertAngles)*self.senseParams["lidarRange"]
            sensorRayZ = np.sin(vertAngles)*self.senseParams["lidarRange"]
            xVec = np.array(p.multiplyTransforms([0,0,0],sensorAbsolutePose[1],[1,0,0],[0,0,0,1])[0])
            yVec = np.array(p.multiplyTransforms([0,0,0],sensorAbsolutePose[1],[0,1,0],[0,0,0,1])[0])
            zVec = np.array(p.multiplyTransforms([0,0,0],sensorAbsolutePose[1],[0,0,1],[0,0,0,1])[0])
            endX = sensorAbsolutePose[0][0]+sensorRayX*xVec[0]+sensorRayY*yVec[0]+sensorRayZ*zVec[0]
            endY = sensorAbsolutePose[0][1]+sensorRayX*xVec[1]+sensorRayY*yVec[1]+sensorRayZ*zVec[1]
            endZ = sensorAbsolutePose[0][2]+sensorRayX*xVec[2]+sensorRayY*yVec[2]+sensorRayZ*zVec[2]
            rayToPositions = np.stack([endX,endY,endZ],axis=0).transpose().tolist()
            rayFromPositions = np.repeat(np.matrix(sensorAbsolutePose[0]),len(rayToPositions),axis=0).tolist()
            rayResults = ()
            while len(rayResults)<len(rayToPositions):
                batchStartIndex = len(rayResults)
                batchEndIndex = batchStartIndex + p.MAX_RAY_INTERSECTION_BATCH_SIZE
                rayResults = rayResults + p.rayTestBatch(rayFromPositions[batchStartIndex:batchEndIndex],rayToPositions[batchStartIndex:batchEndIndex],physicsClientId=self.physicsClientId)
            rangeData = np.array([rayResults[i][2] for i in range(len(rayResults))]).reshape(originalShape)
            if senseType == 1: # return depth map
                sensorData = rangeData
            else: # return point cloud
                lidarPoints = [rayResults[i][3] for i in range(len(rayResults))]
                lidarPoints = np.array(lidarPoints).transpose()
                if self.senseParams["removeInvalidPointsInPC"]:
                    lidarPoints = lidarPoints[:,rangeData.reshape(-1)<1]
                sensorData = lidarPoints
        if expandDim:
            sensorData = np.expand_dims(sensorData,axis=0)
        return sensorData
    # generate random drive action
    def randomDriveAction(self):
        return self.randDrive.next()
        #return self.randDrive.next()*np.array(self.simulationParams['randomActionScale'])
        #self.randDrive.multiGenNoise(50)
        #self.sinActionT = self.sinActionT+np.random.normal([0.1,0.5],[0.01,0.2])
        #return np.sin(self.sinActionT)