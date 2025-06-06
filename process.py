import os
import time
import json
import torch
from scipy import ndimage
import SimpleITK as sitk
import numpy as np
from pathlib import Path
from evalutils import SegmentationAlgorithm
from nnunetv2.inference.predict_from_raw_data import nnUNetPredictor
from xgboost import XGBClassifier




def calculate_real_size(img):
    shape = img.GetSize()
    voxel_spacing = img.GetSpacing()
    real_shape = [round(sh*vs, 1) for sh, vs in zip(shape, voxel_spacing)]
    return tuple(real_shape[::-1])


def calculate_slides(np_img, axis=0):
    assert np_img.shape == (64, 128, 128)
    mean_array = np.mean(np_img, axis=tuple(
        [i for i in range(3) if i != axis]))
    minimum = -1023
    if min(mean_array) < -1024:
        minimum = -2048
    begin = -1
    end = -1
    for index, mean in enumerate(mean_array):
        if not np.isclose(mean, minimum, atol=1) and begin < 0:
            begin = index
        elif begin >= 0 and end < 0 and np.isclose(mean, minimum, atol=1):
            end = index
            break
    if end == -1:
        end = (64 if axis == 0 else 128)
    return end-begin, begin, end


def calculate_unpadded_size(np_img):
    return tuple([calculate_slides(np_img, axis=i)[0] for i in range(3)])


def images_from_numpy(hu_image):
    sitk_image = sitk.GetImageFromArray(hu_image)
    hu_unpadded = hu_image[hu_image > -2048] if np.min(
        hu_image) < -1024 else hu_image[hu_image > -1024]
    # , "Real size" : calculate_real_size(sitk_image)
    stats_dict = {}
    z, x, y = calculate_unpadded_size(hu_image)
    z_real, x_real, y_real = calculate_real_size(sitk_image)
    stats_dict["x_unpadded"] = x
    stats_dict["y_unpadded"] = y
    stats_dict["z_unpadded"] = z
    stats_dict["x_real"] = x_real
    stats_dict["y_real"] = y_real
    stats_dict["z_real"] = z_real
    stats_dict["sum"] = np.sum(hu_unpadded)
    stats_dict["voxels"] = len(hu_unpadded)
    stats_dict["mean"] = np.mean(hu_unpadded)
    stats_dict["std"] = np.std(hu_unpadded)
    stats_dict["median"] = np.median(hu_unpadded)
    stats_dict["min"] = np.min(hu_unpadded)
    stats_dict["max"] = np.max(hu_unpadded)
    hist, _ = np.histogram(hu_unpadded, bins=np.linspace(-1000, 3000, 81))
    for i, h in enumerate(hist):
        stats_dict[f"hist_{int(np.linspace(-1000, 3000, 81)[i])}"] = h / \
            len(hu_unpadded)
    return tuple(stats_dict.values())


class Uls23(SegmentationAlgorithm):
    def __init__(self):
        self.image_metadata = None  # Keep track of the metadata of the input volume
        self.id = None  # Keep track of batched volume file name for export
        self.z_size = 128  # Number of voxels in the z-dimension for each input VOI
        self.xy_size = 256  # Number of voxels in the xy-dimensions for each input VOI
        self.z_size_model = 64  # Number of voxels in the z-dimension that the model takes
        self.xy_size_model = 128  # Number of voxels in the xy-dimensions that the model takes
        self.device = torch.device("cuda")
        self.predictor_other = None  # nnUnet predictor
        self.predictor_pancreas = None
        self.predictor_colon = None
        self.predictor_abdominal = None
        self.estimators = self.load_estimators()

    def load_estimators(self):
        estimators = [XGBClassifier(random_state=42) for i in range(5)]
        for i, e in enumerate(estimators):
            e.load_model(f'/opt/ml/model/xgb_estimators/xgb_{i}.bin')
        return estimators

    def start_pipeline(self):
        """
        Starts inference algorithm
        """
        start_time = time.time()

        # We need to create the correct output folder, determined by the interface, ourselves
        os.makedirs("/output/images/ct-binary-uls/", exist_ok=True)
        self.load_models()
        spacings = self.load_data()
        predictions = self.predict_with_classifier(spacings)
        self.postprocess(predictions)

        end_time = time.time()
        print(f"Total job runtime: {end_time - start_time}s")
        
    def load_model(self, dataset):
        start_model_load_time = time.time()
        print("start")
        # Set up the nnUNetPredictor
        predictor = nnUNetPredictor(
            tile_step_size=0.5,
            use_gaussian=True,
            use_mirroring=False,
            device=self.device,
            verbose=False,
            verbose_preprocessing=False,
            allow_tqdm=False
        )
        # Initialize the network architecture, loads the checkpoint
        predictor.initialize_from_trained_model_folder(
            f"/opt/ml/model/{dataset}/nnUNetTrainer_Lovasz__nnUNetPlans__3d_fullres",
            use_folds=(0,),
            checkpoint_name="checkpoint_best.pth",
        )
        end_model_load_time = time.time()
        print(
            f"Bone model loading runtime: {end_model_load_time - start_model_load_time}s")
        return predictor

    def load_models(self):
        self.predictor_pancreas = self.load_model("Dataset012_diag_pancreasCT")
        self.predictor_colon = self.load_model("Dataset103_Colon")
        self.predictor_other = self.load_model("Dataset109_Other")
        self.predictor_abdominal = self.load_model("Dataset101_Abdominal")

    def load_data(self):
        """
        1) Loads the .mha files containing the VOI stacks in the input directory
        2) Unstacks them into individual lesion VOI's
        3) Optional: preprocess volumes
        4) Predict per VOI
        """
        start_load_time = time.time()

        # Input directory is determined by the algorithm interface on GC
        input_dir = Path("/input/images/stacked-3d-ct-lesion-volumes/")

        # Load the spacings per VOI
        with open(Path("/input/stacked-3d-volumetric-spacings.json"), 'r') as json_file:
            spacings = json.load(json_file)

        for input_file in input_dir.glob("*.mha"):
            self.id = input_file

            # Load and keep track of the image metadata
            self.image_metadata = sitk.ReadImage(input_dir / input_file)

            # Now get the image data
            image_data = sitk.GetArrayFromImage(self.image_metadata)
            for i in range(int(image_data.shape[0] / self.z_size)):
                voi = image_data[self.z_size * i:self.z_size * (i + 1), :, :]

                # Convert the VOI back to a SimpleITK image
                voi_image = sitk.GetImageFromArray(voi)

                # Calculate and set the metadata for the unstacked VOI
                original_origin = self.image_metadata.GetOrigin()
                original_spacing = self.image_metadata.GetSpacing()
                new_origin = [
                    original_origin[0],  # x-origin remains the same
                    original_origin[1],  # y-origin remains the same
                    # Adjust z-origin for each VOI
                    original_origin[2] + i * self.z_size * original_spacing[2],
                ]
                voi_image.SetOrigin(new_origin)
                voi_image.SetSpacing(original_spacing)
                voi_image.SetDirection(self.image_metadata.GetDirection())

                # Define the cropping region in physical space
                voi_shape = voi_image.GetSize()
                start_index = [64, 64, 32]  # Start indices for cropping
                crop_size = [128, 128, 64]  # Size of the cropped region

                # Perform cropping using SimpleITK
                voi_cropped = sitk.RegionOfInterest(
                    voi_image, size=crop_size, index=start_index)

                # Update the origin of the cropped VOI
                cropped_origin = [
                    voi_image.GetOrigin()[0] + start_index[0] *
                    voi_image.GetSpacing()[0],
                    voi_image.GetOrigin()[1] + start_index[1] *
                    voi_image.GetSpacing()[1],
                    voi_image.GetOrigin()[2] + start_index[2] *
                    voi_image.GetSpacing()[2],
                ]
                voi_cropped.SetOrigin(cropped_origin)
                voi_cropped.SetSpacing(voi_image.GetSpacing())
                voi_cropped.SetDirection(voi_image.GetDirection())

                # Save the cropped VOI to a binary file
                voi_cropped_array = sitk.GetArrayFromImage(voi_cropped)
                # Add dummy batch dimension for nnUnet
                np.save(f"/tmp/voi_{i}.npy", np.array([voi_cropped_array]))

        end_load_time = time.time()
        print(
            f"Data pre-processing runtime: {end_load_time - start_load_time}s")

        return spacings

    def predict_with_classifier(self, spacings):
        """
        Runs nnUnet inference on the images, then moves to post-processing
        :param spacings: list containing the spacing per VOI
        :return: list of numpy arrays containing the predicted lesion masks per VOI
        """
        start_inference_time = time.time()
        predictions = []

        for i, voi_spacing in enumerate(spacings):
            # Load the 3D array from the binary file
            voi = np.load(f"/tmp/voi_{i}.npy").astype(np.float32)
            
            # Predict class
            x = images_from_numpy(voi[0, :, :, :])
            class_label = int(np.argmax(
                (sum(estimator.predict_proba([x]) for estimator in self.estimators))))


            print(
                f'\nPredicting image of shape: {voi.shape}, spacing: {voi_spacing}')
            
            match class_label:
                case 0:
                    predictions.append(self.predictor_abdominal.predict_single_npy_array(
                    voi, {'spacing': voi_spacing}, None, None, False))
                case 6:
                    predictions.append(self.predictor_other.predict_single_npy_array(
                    voi, {'spacing': voi_spacing}, None, None, False))
                case 7:
                    predictions.append(self.predictor_pancreas.predict_single_npy_array(
                    voi, {'spacing': voi_spacing}, None, None, False))
                case 8:
                    predictions.append(self.predictor_colon.predict_single_npy_array(
                    voi, {'spacing': voi_spacing}, None, None, False))
                case _:
                    predictions.append(self.predictor_pancreas.predict_single_npy_array(
                    voi, {'spacing': voi_spacing}, None, None, False))

        end_inference_time = time.time()
        print(
            f"Total inference runtime: {end_inference_time - start_inference_time}s")
        return predictions

    def predict(self, spacings):
        """
        Runs nnUnet inference on the images, then moves to post-processing
        :param spacings: list containing the spacing per VOI
        :return: list of numpy arrays containing the predicted lesion masks per VOI
        """
        start_inference_time = time.time()
        predictions = []

        for i, voi_spacing in enumerate(spacings):
            # Load the 3D array from the binary file
            voi = np.load(f"/tmp/voi_{i}.npy").astype(np.float32)

            print(
                f'\nPredicting image of shape: {voi.shape}, spacing: {voi_spacing}')
            predictions.append(self.predictor_pancreas.predict_single_npy_array(
                voi, {'spacing': voi_spacing}, None, None, False))

        end_inference_time = time.time()
        print(
            f"Total inference runtime: {end_inference_time - start_inference_time}s")
        return predictions

    def postprocess(self, predictions):
        """
        Runs post-processing and saves predictions for each VOI.
        :param predictions: list of numpy arrays containing the predicted lesion masks per VOI
        """
        start_postprocessing_time = time.time()

        for i, segmentation in enumerate(predictions):
            print(f"Post-processing prediction {i}")
            instance_mask, num_features = ndimage.label(segmentation)
            if num_features > 1:
                print("Found multiple lesion predictions")
                segmentation[instance_mask != instance_mask[
                    int(self.z_size_model / 2), int(self.xy_size_model / 2), int(self.xy_size_model / 2)]] = 0
                segmentation[segmentation != 0] = 1

            # Pad segmentations to fit with original image size
            segmentation_pad = np.pad(segmentation,
                                      ((32, 32),
                                       (64, 64),
                                          (64, 64)),
                                      mode='constant', constant_values=0)

            # Convert padded segmentation and original segmentation back to a SimpleITK image
            segmentation_image = sitk.GetImageFromArray(segmentation_pad)
            segmentation_original = sitk.GetImageFromArray(segmentation)

            # Update the origin to account for the padding
            voi_origin = segmentation_original.GetOrigin()
            voi_spacing = segmentation_original.GetSpacing()
            voi_direction = segmentation_original.GetDirection()

            new_origin = [
                voi_origin[0] - 32 * voi_spacing[0],  # Adjust for z padding
                voi_origin[1] - 64 * voi_spacing[1],  # Adjust for x padding
                voi_origin[2] - 64 * voi_spacing[2],  # Adjust for y padding
            ]
            segmentation_image.SetOrigin(new_origin)
            segmentation_image.SetDirection(voi_direction)
            segmentation_image.SetSpacing(voi_spacing)

            # Save the updated segmentation image
            predictions[i] = sitk.GetArrayFromImage(segmentation_image)

        predictions = np.concatenate(predictions, axis=0)  # Stack predictions

        # Create mask image and copy over metadata
        mask = sitk.GetImageFromArray(predictions)
        mask.CopyInformation(self.image_metadata)

        sitk.WriteImage(mask, f"/output/images/ct-binary-uls/{self.id.name}")
        print("Output dir contents:", os.listdir(
            "/output/images/ct-binary-uls/"))
        print("Output batched image shape:", predictions.shape)
        end_postprocessing_time = time.time()
        print(
            f"Postprocessing & saving runtime: {end_postprocessing_time - start_postprocessing_time}s")


if __name__ == "__main__":
    Uls23().start_pipeline()
